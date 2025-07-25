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
    logger.error("BOT_USERNAME field is missing in .env")
    raise RuntimeError("BOT_USERNAME field is missing in .env")

SECRET_TOKEN = os.getenv("SECRET_TOKEN")
if not SECRET_TOKEN:
    logger.error("SECRET_TOKEN field is missing in .env")
    raise RuntimeError("SECRET_TOKEN field is missing in .env")

GROUP_ID = os.getenv("GROUP_ID")
GROUP_LINK = os.getenv("GROUP_LINK")
REFERRAL_MANDATORY_COUNT = os.getenv("REFERRAL_MANDATORY_COUNT")
try:
    GROUP_ID = int(GROUP_ID)
    REFERRAL_MANDATORY_COUNT = int(REFERRAL_MANDATORY_COUNT)
except ValueError as e:
    logger.error("could not transfer types str -> int for GROUP_ID, REFERRAL_MANDATORY_COUNT. Closing application...")
    sys.exit(1)

# Telegram constants
COMMANDS = [
    BotCommand("help", "Yordam"),
    BotCommand("start", "Ro‘yxatdan o‘tish"),
    BotCommand("my_refs", "Mening referallarim")
]

# Cache
_prev_can_write: dict[int, bool] = {}
_joined_users: dict[int, datetime] = {}

# Rate limit interval
RATE_LIMIT_INTERVAL = timedelta(seconds=10)
DATABASE_SEMAPHORE = asyncio.Semaphore(10) # <- Here put value of pool_size in db engine
BATCH_SIZE = 50
SLEEP_BETWEEN_BATCHES = 0.5
CHUNK_SIZE = 50


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
        maxBytes=10*1024*1024, # 10Mb
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
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
            text=f"Assalomu alaykum! Siz @{BOT_USERNAME} ni ushbu guruhga referal tizimini o‘rnatishim uchun chaqirdingiz."
                 f"\n\n❗️ To‘g‘ri ishlashim uchun, iltimos, meni ushbu guruhda administrator qilib tayinlang."
        )
    if old.status == "member" and new.status == "administrator":
        await send_effective_chat_message(
            update=update,
            text="✅ Rahmat! Endi men ishga tayyorman."
        )
    return

# Handle new user enters group
async def new_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.debug("new_chat_member_update: called, chat_id=%s", update.effective_chat.id)
    now = datetime.utcnow()
    chat_id = None
    new_users = []

    # MESSAGE UPDATE
    if update.message and update.message.new_chat_members:
        logger.debug("new_chat_member_update: type=MESSAGE UPDATE")
        chat_id = update.effective_chat.id
        new_users = update.message.new_chat_members

    # MEMBER UPDATE
    elif update.chat_member:
        logger.debug("new_chat_member_update: type=MEMBER UPDATE")
        member = update.chat_member
        chat_id = member.chat.id
        old, new = member.old_chat_member, member.new_chat_member
        logger.debug(f"new_chat_member_update: STATUSES - OLD={old} NEW={new}")
        if chat_id != GROUP_ID:
            return
        if old.status in ("left", "kicked") and new.status in ("member", "restricted"):
            new_users = [new.user]
        else:
            logger.debug("new_chat_member_update: member with chat_id=%s is not new member", update.effective_chat.id)
            return

    if chat_id != GROUP_ID or not new_users:
        return

    for user in new_users:
        # Deduplication
        last = _joined_users.get(user.id)
        if last and now - last < timedelta(seconds=30):
            logger.debug("new_chat_member_update: skip duplicated join for %s", user.id)
            continue
        _joined_users[user.id] = now

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
            logger.warning("new_chat_member_update: user already exists")
            pass
        except DatabaseConnectionError:
            logger.warning("new_chat_member_update: could not connect to database")
            return

        try:
            async with DATABASE_SEMAPHORE:
                registered = await is_registered(user_id)
        except DatabaseConnectionError:
            logger.warning("new_chat_member_update: could not connect to database")
            return
        can_write = False
        if not registered:
            await send_effective_chat_message(
                update=update,
                text=f"Salom @{username}, {update.effective_chat.title} ga xush kelibsiz!"
                     f"\n\nUshbu guruhda post joylash huquqini olish uchun, iltimos, quyidagi havola orqali ro‘yxatdan o‘ting "
                     f"mana bu havola orqali: https://t.me/{BOT_USERNAME}?start"
            )
        else:
            try:
                async with DATABASE_SEMAPHORE:
                    ref_count = await count_referrals(user_id)
            except DatabaseConnectionError:
                logger.warning("new_chat_member_update: could not connect to database")
                return
            can_write = ref_count >= REFERRAL_MANDATORY_COUNT
            await send_effective_chat_message(
                update=update,
                text=f"Yana qaytib kelganingiz bilan, @{username}, xush kelibsiz!"
            )
        success = await set_permissions(context, GROUP_ID, user_id, can_write)
        if not success:
            logger.debug("new_chat_member_update: finished with failure")
            return
    logger.debug("new_chat_member_update: successfully finished")


async def chat_member_periodic_update(context: ContextTypes.DEFAULT_TYPE):
    logger.debug("chat_member_periodic_update: called")

    await send_default_context_message(
        context=context,
        text="Hurmatli foydalanuvchilar, eslatib o‘tamiz, guruhimizda referal dasturi amal qiladi."
             "\n\nGuruhda post joylash huquqini faollashtirish uchun sizga quyidagilar kerak:"
             f"\n1) Men bilan shaxsiy chatda https://t.me/{BOT_USERNAME}?start manzili orqali ro‘yxatdan o‘ting"
             f"\n2) Guruhga {REFERRAL_MANDATORY_COUNT} nafar odamni taklif qiling va ularni referal havolangiz orqali ro‘yxatdan o‘tkazing"
             " – bu havolani siz o‘zingiz ro‘yxatdan o‘tganingizda olgansiz."
             f"\n\n\n⚠️ MUHIM ⚠️\n\nYangi referalni tizim hisobga olishi uchun, avvalo u guruhga qo‘shilishi kerak,"
             f" so‘ngra esa sizning referal havolangiz orqali ro‘yxatdan o‘tishi zarur.\nAks holda u hisoblanmaydi!",
        chat_id=GROUP_ID
    )

    try:
        async with DATABASE_SEMAPHORE:
            user_ids = await get_included_user_ids()
    except DatabaseConnectionError:
        logger.warning("chat_member_periodic_update: could not connect to database")
        return
    if not user_ids:
        logger.debug("chat_member_periodic_update: finished -> no chat members included")
        return
    try:
        async with DATABASE_SEMAPHORE:
            user_referral_counts = await get_registered_referral_counts(user_ids, CHUNK_SIZE)
    except DatabaseConnectionError:
        logger.warning("chat_member_periodic_update: could not connect to database")
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
                success = await set_permissions(context, GROUP_ID, uid, is_allowed)
                if not success:
                    pass
            tasks.append(asyncio.create_task(proc(user_id, can_write)))
        if tasks:
            await asyncio.gather(*tasks)
        await asyncio.sleep(SLEEP_BETWEEN_BATCHES)
    logger.debug("chat_member_periodic_update: finished")

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
        logger.error("chat_member_periodic_update: could not perform the operation for user %s due to %s", telegram_id, e)
        await send_reply_message(
            update=update,
            text="❌ Kechirasiz, noma’lum sabablarga ko‘ra amaliyotni bajarib bo‘lmadi. Iltimos, keyinroq yana urinib ko‘ring!"
        )
        return
    if chat_member.status not in ("creator", "administrator", "member", "restricted"):
        await send_reply_message(
            update=update,
            text=f"❕ Ro‘yxatdan o‘tish uchun siz ushbu guruh a’zosi bo‘lishingiz kerak: {GROUP_LINK}"
        )
        return

    # Save user
    async with DATABASE_SEMAPHORE:
        try:
            await include_user(telegram_id)
        except UserAlreadyExists:
            logger.warning("start: user already exists -> GroupUserModel")
            pass
        except DatabaseConnectionError:
            logger.warning("start: could not connect to database -> GroupUserModel")
            await send_reply_message(
                update=update,
                text="❌ Kechirasiz, noma’lum sabablarga ko‘ra amaliyotni bajarib bo‘lmadi. Iltimos, keyinroq yana urinib ko‘ring!"
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
                    logger.warning("start: could not connect to database")
                    return
                if ref_count >= REFERRAL_MANDATORY_COUNT:
                    success = await set_permissions(context, GROUP_ID, inviter_id, True)
                    if not success:
                        return
        except UserAlreadyExists:
            logger.warning("start: user already exists -> UserModel")
            await send_reply_message(
                update=update,
                text="❌ Siz allaqachon ro‘yxatdan o‘tgansiz!"
            )
            return
        except DatabaseConnectionError:
            logger.warning("start: could not connect to database -> UserModel")
            await send_reply_message(
                update=update,
                text="❌ Kechirasiz, noma’lum sabablarga ko‘ra amaliyotni bajarib bo‘lmadi. Iltimos, keyinroq yana urinib ko‘ring!"
            )
            return

        ref_link = f"https://t.me/{BOT_USERNAME}?start={user.referral_code}"
        await send_reply_message(
            update=update,
            text=f"✅ Siz muvaffaqiyatli ro‘yxatdan o‘tdingiz!\n\n❗️ Guruhda referal dasturi amal qiladi ❗️"
                 f"\nXabar yuborish huquqini olish uchun, iltimos, {REFERRAL_MANDATORY_COUNT} ta foydalanuvchini"
                 f" guruhga taklif qiling va ulardan sizning referal havolangiz orqali ( {ref_link} ) ro‘yxatdan o‘tishni so‘rang"
                 f"\n\n\n⚠️ MUHIM ⚠️\n\nYangi referalni tizim hisobga olishi uchun, avvalo u guruhga qo‘shilishi kerak,"
                 f" so‘ngra esa sizning referal havolangiz orqali ro‘yxatdan o‘tishi zarur.\nAks holda u hisoblanmaydi!"
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
        logger.warning("my_refs: could not connect to database")
        await send_reply_message(
            update=update,
            text="❌ Kechirasiz, noma’lum sabablarga ko‘ra amaliyotni bajarib bo‘lmadi. Iltimos, keyinroq yana urinib ko‘ring!"
        )
        return
    if not registered:
        await send_reply_message(
            update=update,
            text="❌ Siz hali ro‘yxatdan o‘tmagansiz"
        )
        return
    else:
        try:
            async with DATABASE_SEMAPHORE:
                ref_count = await count_referrals(telegram_id)
        except DatabaseConnectionError:
            logger.warning("my_refs: could not connect to database")
            await send_reply_message(
                update=update,
                text="❌ Kechirasiz, noma’lum sabablarga ko‘ra amaliyotni bajarib bo‘lmadi. Iltimos, keyinroq yana urinib ko‘ring!"
            )
        ref_left = REFERRAL_MANDATORY_COUNT - ref_count
        if ref_left < 0:
            ref_left = 0
        await send_reply_message(
            update=update,
            text=f"Sizning referallaringiz soni: {ref_count}. Huquqni olish uchun yana {ref_left} nafar odam taklif qilishingiz kerak!"
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
        text="🔧 Buyruqlar ro‘yxati:\n\n/start - Ro‘yxatdan o‘tish\n/my_refs - Referallarni ko‘rish"
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
        app.job_queue.run_repeating(chat_member_periodic_update, interval=3600, first=10)
        logger.info("on_startup_hook: database and tasks ran successfully")
    except Exception as e:
        logger.critical("on_startup_hook: database and tasks run error %s", e, exc_info=True)
        sys.exit(1)


""" Main """

def main():
    # Logging
    setup_logging()

    try:
        # Application
        app = (
            ApplicationBuilder()
            .token(SECRET_TOKEN)
            .get_updates_connect_timeout(10)
            .get_updates_read_timeout(20)
            .get_updates_write_timeout(20)
            .get_updates_pool_timeout(5)
            .post_init(on_startup_hook)
            .build())

        # Handlers
        app.add_handler(ChatMemberHandler(self_chat_member_update, chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER))
        app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_chat_member_update))
        app.add_handler(ChatMemberHandler(new_chat_member_update, chat_member_types=ChatMemberHandler.CHAT_MEMBER))
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("my_refs", my_refs))
        app.add_handler(CommandHandler("help", cmd_help))

        # Polling
        app.run_polling(drop_pending_updates=False, allowed_updates=["message", "chat_member", "my_chat_member"])
    except Exception as e:
        logger.critical("could not run application due to %s", e, exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
