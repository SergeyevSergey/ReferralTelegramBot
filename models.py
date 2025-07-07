"""
Models file | all models stored here
"""
from sqlalchemy.orm import declarative_base, relationship, backref
from sqlalchemy import Column, Integer, String, ForeignKey, event, BigInteger
import secrets

Base = declarative_base()


# Models:

class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String)
    referral_code = Column(String(32), unique=True, nullable=False, index=True)
    inviter_id = Column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="SET NULL"),
        nullable=True
    )
    referrals = relationship(
        "UserModel",
        backref=backref("parent", remote_side=[telegram_id]),
        cascade="save-update, merge",
        passive_deletes=True,
    )


class GroupUserModel(Base):
    __tablename__ = "group_users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)


@event.listens_for(UserModel, "before_insert")
def generate_referral_code(mapper, connection, target):
    if not target.referral_code:
        target.referral_code = secrets.token_hex(16)
