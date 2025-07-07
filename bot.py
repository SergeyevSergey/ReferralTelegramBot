"""
Application file | all commands, pools and handlers executes here
"""
import logging
import asyncio
import os
import sys
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from telegram import Update, ChatMember, BotCommand, BotCommandScopeAllPrivateChats
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder, CommandHandler, ChatMemberHandler, ContextTypes, MessageHandler, filters
from db import (register_user, init_database, UserAlreadyExists, count_referrals, is_registered, include_user,
                get_included_user_ids, DatabaseConnectionError, get_registered_referral_counts)
from messages import send_reply_message, send_effective_chat_message, send_default_context_message
from datetime import datetime, timezone, timedelta
from utils import set_permissions

logger = logging.getLogger(__name__)
load_dotenv()

# .env constants
BOT_USERNAME = os.getenv("BOT_USERNAME")
if not BOT_USERNAME:
    logger.error("bot/: BOT_USERNAME field is missing in .env")
    raise RuntimeError("BOT_USERNAME field is missing in .env")

SECRET_TOKEN = os.getenv("SECRET_TOKEN")
if not SECRET_TOKEN:
    logger.error("bot/: SECRET_TOKEN field is missing in .env")
    raise RuntimeError("SECRET_TOKEN field is missing in .env")

GROUP_ID = os.getenv("GROUP_ID")
REFERRAL_MANDATORY_COUNT = os.getenv("REFERRAL_MANDATORY_COUNT")
try:
    GROUP_ID = int(GROUP_ID)
    REFERRAL_MANDATORY_COUNT = int(REFERRAL_MANDATORY_COUNT)
except ValueError as e:
    logger.error("bot/: could not transfer types str -> int for GROUP_ID, REFERRAL_MANDATORY_COUNT. Closing application...")
    sys.exit(1)

# Telegram constants
COMMANDS = [
    BotCommand("help", "Помощь"),
    BotCommand("start", "Зарегистрироваться"),
    BotCommand("my_refs", "Мои рефералы")
]

# Cache
_prev_can_write: dict[int, bool] = {}

# Rate limit interval
RATE_LIMIT_INTERVAL = timedelta(seconds=10)
DATABASE_SEMAPHORE = asyncio.Semaphore(10) # <- Here put value of pool_size in db engine
TELEGRAM_SEMAPHORE = asyncio.Semaphore(10) # <- Better keep in range 5-10
BATCH_SIZE = 100
SLEEP_BETWEEN_BATCHES = 0.5
CHUNK_SIZE = 500


""" Logging setup """

def setup_logging():
    fmt = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))

    # File handler
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        filename=os.path.join(log_dir,"bot.log"),
        maxBytes=1*1024*1024, # 1Mb
        backupCount=3,
        encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console_handler)
    root.addHandler(file_handler)

    logging.getLogger("telegram").setLevel(logging.WARNING)


""" Group membership """

# Handle bot presence in group
async def self_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != GROUP_ID:
        return

    old: ChatMember = update.my_chat_member.old_chat_member
    new: ChatMember = update.my_chat_member.new_chat_member

    if old.status in ("left", "kicked") and new.status == "member":
        await send_effective_chat_message(
            update=update,
            text=f"Здравствуйте! Вы призвали @{BOT_USERNAME} для того чтобы я установил систему рефералов в этой группе."
                 f"\n\n❗️ Для моей корректной работы пожалуйста, назначьте меня администратором в этой группе."
        )
    if old.status == "member" and new.status == "administrator":
        await send_effective_chat_message(
            update=update,
            text="✅ Спасибо! Теперь я готов работать."
        )
    return

# Handle new user enters group
async def new_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.debug("bot/new_chat_member_update: called, chat_id=%s", update.effective_chat.id)
    if update.effective_chat.id != GROUP_ID:
        return

    for user in update.message.new_chat_members:
        # If bot joined group: skip
        if user.username == BOT_USERNAME:
            continue

        user_id = user.id
        username = user.username or ""

        # If first time in database: include in group users
        # Else: skip this step
        try:
            async with DATABASE_SEMAPHORE:
                await include_user(user_id)
        except UserAlreadyExists:
            logger.warning("bot/new_chat_member_update: user already exists")
            pass
        except DatabaseConnectionError:
            logger.warning("bot/new_chat_member_update: could not connect to database")
            return

        try:
            async with DATABASE_SEMAPHORE:
                registered = await is_registered(user_id)
        except DatabaseConnectionError:
            logger.warning("bot/new_chat_member_update: could not connect to database")
            return
        can_write = False
        if not registered:
            await send_effective_chat_message(
                update=update,
                text=f"Здравствуй @{username}, Добро пожаловать в {update.effective_chat.title}!"
                     f"\n\nЧтобы получить право размещать свои посты в этой группе, пожалуйста, зарегистрируйся по "
                     f"этой ссылке: https://t.me/{BOT_USERNAME}?start"
            )
        else:
            try:
                async with DATABASE_SEMAPHORE:
                    ref_count = await count_referrals(user_id)
            except DatabaseConnectionError:
                logger.warning("bot/new_chat_member_update: could not connect to database")
                return
            can_write = ref_count >= REFERRAL_MANDATORY_COUNT
            await send_effective_chat_message(
                update=update,
                text=f"Добро пожаловать обратно, @{username}!"
            )
        success = await set_permissions(context, GROUP_ID, user_id, can_write)
        if not success:
            return
    logger.debug("bot/new_chat_member_update: finished, chat_id=%s", update.effective_chat.id)


async def chat_member_periodic_update(context: ContextTypes.DEFAULT_TYPE):
    logger.debug("bot/chat_member_periodic_update: called")

    try:
        async with DATABASE_SEMAPHORE:
            user_ids = await get_included_user_ids()
    except DatabaseConnectionError:
        logger.warning("bot/chat_member_periodic_update: could not connect to database")
        return
    if not user_ids:
        logger.debug("bot/chat_member_periodic_update: finished -> no chat members included")
        return
    try:
        async with DATABASE_SEMAPHORE:
            user_referral_counts = await get_registered_referral_counts(user_ids, CHUNK_SIZE)
    except DatabaseConnectionError:
        logger.warning("bot/chat_member_periodic_update: could not connect to database")
        return

    for i in range(0, len(user_ids), BATCH_SIZE):
        batch = user_ids[i:i+BATCH_SIZE]
        tasks = []
        for user_id in batch:
            count = user_referral_counts.get(user_id)
            if count is None:
                can_write = False
            else:
                can_write = count >= REFERRAL_MANDATORY_COUNT

            async def proc(uid, is_allowed):
                async with TELEGRAM_SEMAPHORE:
                    success = await set_permissions(context, GROUP_ID, uid, is_allowed)
                    if not success:
                        pass
            tasks.append(asyncio.create_task(proc(user_id, can_write)))
        if tasks:
            await asyncio.gather(*tasks)
        await asyncio.sleep(SLEEP_BETWEEN_BATCHES)
    await send_default_context_message(
        context=context,
        text="Уважаемые пользователи, напоминаю вам что в нашей группе действует реферальная программа."
             "\n\nЧтобы разблокировать право размещать посты в этой группе, необходимо:"
             f"\n1) Зарегистрируйтесь через личный чат со мной https://t.me/{BOT_USERNAME}?start"
             f"\n2) Пригласите {REFERRAL_MANDATORY_COUNT} человек в группу и зарегистрируйте их через свою реферальную ссылку"
             " которую вы получили при собственной регистрации."
             f"\n\n\n⚠️ ВАЖНО ⚠️\n\nЧтобы вам засчитало регистрацию нового реферала, он должен сначала вступить в группу,"
             f" а затем зарегистрироваться по вашей реферальной ссылке.\nИначе не сработает!",
        chat_id=GROUP_ID
    )
    logger.debug("bot/chat_member_periodic_update: finished")

""" Commands """

# /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Ignores command messages written while bot in offline
    startup_time = context.bot_data.get("startup_time")
    msg_time = update.message.date
    if startup_time and msg_time < startup_time:
        return

    # Rate limit
    now = datetime.utcnow()
    last_start_time = context.user_data.get("last_start_time")
    if last_start_time and now - last_start_time < RATE_LIMIT_INTERVAL:
        return
    context.user_data["last_start_time"] = now

    telegram_id = update.effective_user.id
    username = update.effective_user.username or ""
    ref_code = context.args[0] if context.args else None

    # Check whether user consists in group
    try:
        chat_member = await context.bot.get_chat_member(GROUP_ID, telegram_id)
    except TelegramError as e:
        logger.error("bot/chat_member_periodic_update: could not perform the operation for user %s due to %s", telegram_id, e)
        await send_reply_message(
            update=update,
            text="❌ Простите, мы не смогли выполнить операцию по неопределенным причинам. Пожалуйста, попробуйте позже!"
        )
        return
    if chat_member.status not in ("creator", "administrator", "member", "restricted"):
        try:
            invite_link = await context.bot.export_chat_invite_link(GROUP_ID)
        except TelegramError as e:
            logger.error("bot/chat_member_periodic_update: could not perform the operation for user %s due to %s", telegram_id, e)
            await send_reply_message(
                update=update,
                text="❌ Простите, мы не смогли выполнить операцию по неопределенным причинам. Пожалуйста, попробуйте позже!"
            )
            return
        await send_reply_message(
            update=update,
            text=f"❕ Чтобы зарегистрироваться вы должны состоять в группе: {invite_link}"
        )
        return

    # Save user
    async with DATABASE_SEMAPHORE:
        try:
            await include_user(telegram_id)
        except UserAlreadyExists:
            logger.warning("bot/start: user already exists -> GroupUserModel")
            pass
        except DatabaseConnectionError:
            logger.warning("bot/start: could not connect to database -> GroupUserModel")
            await send_reply_message(
                update=update,
                text="❌ Простите, мы не смогли выполнить операцию по неопределенным причинам. Пожалуйста, попробуйте позже!"
            )
            return
        try:
            result = await register_user(telegram_id, username, ref_code)
            user = result[0]
            inviter_id = result[1]
            if inviter_id:
                try:
                    ref_count = await count_referrals(inviter_id)
                except DatabaseConnectionError:
                    logger.warning("bot/start: could not connect to database")
                    return
                if ref_count >= REFERRAL_MANDATORY_COUNT:
                    success = await set_permissions(context, GROUP_ID, inviter_id, True)
                    if not success:
                        return
        except UserAlreadyExists:
            logger.warning("bot/start: user already exists -> UserModel")
            await send_reply_message(
                update=update,
                text="❌ Вы уже и так зарегистрированы!"
            )
            return
        except DatabaseConnectionError:
            logger.warning("bot/start: could not connect to database -> UserModel")
            await send_reply_message(
                update=update,
                text="❌ Простите, мы не смогли выполнить операцию по неопределенным причинам. Пожалуйста, попробуйте позже!"
            )
            return

        ref_link = f"https://t.me/{BOT_USERNAME}?start={user.referral_code}"
        await send_reply_message(
            update=update,
            text=f"✅ Вы были успешно зарегистрированы!\n\n❗️ В группе действует реферальная программа ❗️"
                 f"\nЧтобы получить доступ к отправке сообщений, пожалуйста, пригласите {REFERRAL_MANDATORY_COUNT}"
                 f" пользователей в группу и попросите зарегистрироваться через меня по вашей реферальной ссылке: {ref_link}"
                 f"\n\n\n⚠️ ВАЖНО ⚠️\n\nЧтобы вам засчитало регистрацию нового реферала, он должен сначала вступить в группу,"
                 f" а затем зарегистрироваться по вашей реферальной ссылке.\nИначе не сработает!"
        )


# /my_refs
async def my_refs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Ignores command messages written while bot in offline
    startup_time = context.bot_data.get("startup_time")
    msg_time = update.message.date
    if startup_time and msg_time < startup_time:
        return

    # Rate limit
    now = datetime.utcnow()
    last_start_time = context.user_data.get("last_start_time")
    if last_start_time and now - last_start_time < RATE_LIMIT_INTERVAL:
        return
    context.user_data["last_start_time"] = now

    telegram_id = update.effective_user.id
    try:
        async with DATABASE_SEMAPHORE:
            registered = await is_registered(telegram_id)
    except DatabaseConnectionError:
        logger.warning("bot/my_refs: could not connect to database")
        await send_reply_message(
            update=update,
            text="❌ Простите, мы не смогли выполнить операцию по неопределенным причинам. Пожалуйста, попробуйте позже!"
        )
        return
    if not registered:
        await send_reply_message(
            update=update,
            text="❌ Вы еще не регистрировались"
        )
        return
    else:
        try:
            async with DATABASE_SEMAPHORE:
                ref_count = await count_referrals(telegram_id)
        except DatabaseConnectionError:
            logger.warning("bot/my_refs: could not connect to database")
            await send_reply_message(
                update=update,
                text="❌ Простите, мы не смогли выполнить операцию по неопределенным причинам. Пожалуйста, попробуйте позже!"
            )
        ref_left = REFERRAL_MANDATORY_COUNT - ref_count
        if ref_left < 0:
            ref_left = 0
        await send_reply_message(
            update=update,
            text=f"Количество ваших рефералов: {ref_count}. До получения прав вам осталось пригласить {ref_left} человек!"
        )


# /help
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Ignores command messages written while bot in offline
    startup_time = context.bot_data.get("startup_time")
    msg_time = update.message.date
    if startup_time and msg_time < startup_time:
        return

    # Rate limit
    now = datetime.utcnow()
    last_start_time = context.user_data.get("last_start_time")
    if last_start_time and now - last_start_time < RATE_LIMIT_INTERVAL:
        return
    context.user_data["last_start_time"] = now

    await send_reply_message(
        update=update,
        text="🔧 Вот список команд:\n\n/start - Регистрация\n/my_refs - Просмотр рефералов"
    )


""" Hooks """

async def on_startup_hook(app):
    try:
        await init_database()
        await app.bot.set_my_commands(
            commands=COMMANDS,
            scope=BotCommandScopeAllPrivateChats()
        )
        app.bot_data["startup_time"] = datetime.now(timezone.utc)
        app.job_queue.run_repeating(chat_member_periodic_update, interval=60, first=60)
        logger.info("bot/on_startup_hook: database and tasks ran successfully")
    except Exception as e:
        logger.critical("bot/on_startup_hook: database and tasks run error %s", e, exc_info=True)
        sys.exit(1)


""" Main """

def main():
    # Logging
    setup_logging()

    try:
        # Application
        app = ApplicationBuilder().token(SECRET_TOKEN).post_init(on_startup_hook).build()

        # Handlers
        app.add_handler(ChatMemberHandler(self_chat_member_update, chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER))
        app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_chat_member_update))
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("my_refs", my_refs))
        app.add_handler(CommandHandler("help", cmd_help))

        # Polling
        app.run_polling(drop_pending_updates=False, allowed_updates=["message", "chat_member", "my_chat_member"])
    except Exception as e:
        logger.critical("bot/: could not run application due to %s", e, exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
