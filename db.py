"""
Database file | all operations on database executes here
"""
import os
import logging
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, and_, func
from dotenv import load_dotenv
from models import UserModel, Base, GroupUserModel

logger = logging.getLogger(__name__)
load_dotenv()

REQUIRED_ENV_VARS = [
    "DB_USER",
    "DB_PASSWORD",
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
]

missing = []
for var in REQUIRED_ENV_VARS:
    if not os.getenv(var):
        missing.append(var)

if missing:
    logger.error(f"db/: Database properties are missing in .env: {missing} fields")
    raise RuntimeError(f"Database properties are missing in .env: {missing} fields")

DATABASE_URL = (
    "postgresql+asyncpg://"
    f"{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@"
    f"{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/"
    f"{os.getenv('DB_NAME')}"
)


# Database session initialization
engine = create_async_engine(
    DATABASE_URL,
    echo=True,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
    pool_recycle=1800
)
db_session = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

# Exceptions

class UserAlreadyExists(Exception):
    pass

class DatabaseConnectionError(Exception):
    pass


# Initialization

async def init_database():
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            logger.info("db/init_database: database tables successfully initialized")
    except Exception as e:
        logger.exception("db/init_database: database initialization error %s", e)
        raise


# Conventions (please use only in this file)

async def _find_inviter(session: AsyncSession, telegram_id: int, ref_code: str | None) -> int | None:
    logger.debug("db/_find_inviter: started for %s with ref_code=%s", telegram_id, ref_code)
    if ref_code:
        inviter_id = await session.scalar(
            select(UserModel.telegram_id)
            .where(
                and_(
                    UserModel.referral_code == ref_code,
                    UserModel.telegram_id != telegram_id
                )
            )
        )
        logger.debug(
            "db/_find_inviter: finished with inviter %s for user %s with ref_code=%s",
            inviter_id, telegram_id, ref_code
        )
        return inviter_id
    else:
        logger.debug("db/_find_inviter: finished with could not find any inviter with ref_code=%s", ref_code)
        return None


# Functions

async def is_registered(telegram_id: int) -> bool:
    logger.debug("db/is_registered: started for user %s", telegram_id)
    try:
        async with db_session() as session:
            user_id = await session.scalar(
                select(UserModel.id)
                .where(
                    UserModel.telegram_id == telegram_id
                )
            )
            logger.debug("db/is_registered: finished for user %s with result: %s", telegram_id, user_id)
            return user_id is not None
    except OperationalError as e:
        logger.exception("db/is_registered: database connection error %s for user %s", e, telegram_id)
        raise DatabaseConnectionError()


async def count_referrals(telegram_id: int) -> int:
    logger.debug("db/count_referrals: started for user %s", telegram_id)
    try:
        async with db_session() as session:
            count = await session.scalar(
                select(func.count()).select_from(UserModel)
                .where(
                    UserModel.inviter_id == telegram_id
                )
            )
            logger.debug("db/count_referrals: finished for user %s with result: %s", telegram_id, count)
            return count or 0
    except OperationalError as e:
        logger.exception("db/count_referrals: database connection error %s for user %s", e, telegram_id)
        raise DatabaseConnectionError()


async def get_registered_referral_counts(user_ids, chunk_size) -> dict:
    logger.debug("db/get_registered_referral_counts: started")
    registered_ids: set[int] = set()
    referral_counts: dict[int, int] = {}
    try:
        async with db_session() as session:
            for i in range(0, len(user_ids), chunk_size):
                chunk = user_ids[i:i+chunk_size]
                rows = await session.execute(
                    select(UserModel.telegram_id)
                    .where(UserModel.telegram_id.in_(chunk))
                )
                registered_ids |= {r[0] for r in rows.all()}
            if not registered_ids:
                logger.debug("db/get_registered_referral_counts: finished -> no registered users found")
                return {}
            rows = await session.execute(
                select(UserModel.inviter_id, func.count().label("count"))
                .where(UserModel.inviter_id.in_(list(registered_ids)))
                .group_by(UserModel.inviter_id)
            )
            for inviter_id, count in rows.all():
                referral_counts[inviter_id] = count or 0
            result = {user_id: referral_counts.get(user_id, 0) for user_id in registered_ids}
            logger.debug("db/get_registered_referral_counts: finished with result %s", result)
            return result
    except OperationalError as e:
        logger.exception("db/get_registered_referral_counts: database connection error %s", e)
        raise DatabaseConnectionError()


async def register_user(telegram_id: int, username: str, ref_code: str | None) -> (UserModel, int):
    logger.debug("db/register_user: started for user %s", telegram_id)
    try:
        async with db_session() as session:
            try:
                async with session.begin():
                    inviter_id = await _find_inviter(session, telegram_id, ref_code)
                    new_user = UserModel(telegram_id=telegram_id, username=username, inviter_id=inviter_id)
                    session.add(new_user)
                await session.refresh(new_user)
                logger.debug(
                    "db/register_user: successfully registered user %s as object %s",
                    telegram_id, new_user
                )
                return [new_user, inviter_id]

            except IntegrityError as e:
                logger.warning(
                    "db/register_user: could not create new user object due to IntegrityError %s",
                    e
                )
                await session.rollback()
                raise UserAlreadyExists()
    except OperationalError as e:
        logger.exception("db/register_user: database connection error %s for user %s", e, telegram_id)
        raise DatabaseConnectionError()


async def include_user(telegram_id: int) -> None:
    logger.debug("db/include_user: started for user %s", telegram_id)
    try:
        async with db_session() as session:
            try:
                async with session.begin():
                    new_user = GroupUserModel(telegram_id=telegram_id)
                    session.add(new_user)
                await session.refresh(new_user)
                logger.debug(
                    "db/include_user: successfully included user %s as object %s",
                    telegram_id, new_user
                )
                return

            except IntegrityError as e:
                logger.warning(
                    "db/include_user: could not create new user object due to IntegrityError %s",
                    e
                )
                await session.rollback()
                raise UserAlreadyExists()
    except OperationalError as e:
        logger.exception("db/include_user: database connection error %s for user %s", e, telegram_id)
        raise DatabaseConnectionError()


async def get_included_user_ids() -> list:
    logger.debug("db/get_included_user_ids: started")
    try:
        async with db_session() as session:
            query = await session.execute(select(GroupUserModel.telegram_id))
            user_ids = [row[0] for row in query.all()]
            logger.debug("db/get_included_user_ids: finished with result: %s", user_ids)
            return user_ids
    except OperationalError as e:
        logger.exception("db/get_included_user_ids: database connection error %s", e)
        raise DatabaseConnectionError()
