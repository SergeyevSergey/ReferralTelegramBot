"""
Utils file | all additional instructions executes here
"""
import logging
import asyncio
from telegram import ChatPermissions
from telegram.ext import ContextTypes
from telegram.error import TelegramError, RetryAfter

logger = logging.getLogger(__name__)

TELEGRAM_SEMAPHORE = asyncio.Semaphore(5) # Better keep in range 5-10

# Utils

async def set_permissions(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, is_allowed: bool) -> bool:
    logger.debug(
        "set_permissions: function started in context=%s with values chat_id=%s, user_id=%s, is_allowed=%s",
        context, chat_id, user_id, is_allowed
    )
    try:
        chat_member = await context.bot.get_chat_member(chat_id, user_id)
    except TelegramError as e:
        logger.warning("set_permissions: failed to set permissions for user %s: %s", user_id, e)
        return False
    if chat_member.status in ("creator", "administrator"):
        logger.debug("set_permissions: finished due to user %s is creator/admin", user_id)
        return True

    permissions = ChatPermissions(
        can_send_messages=is_allowed,
        can_send_other_messages=is_allowed,
        can_send_audios=is_allowed,
        can_send_photos=is_allowed,
        can_send_videos=is_allowed,
        can_add_web_page_previews=is_allowed,
        can_send_documents=is_allowed,
        can_send_video_notes=is_allowed,
        can_send_voice_notes=is_allowed,
        can_send_polls=is_allowed,
    )
    async with TELEGRAM_SEMAPHORE:
        for attempt in range(2):
            try:
                await context.bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    permissions=permissions
                )
                logger.debug("set_permissions: permissions set for user %s: %s on attempt %d", user_id, is_allowed, attempt+1)
                return True
            except RetryAfter as e:
                wait_time = e.retry_after+1
                logger.debug("set_permissions: flood control for user %s, retrying in %s", user_id, wait_time)
                await asyncio.sleep(wait_time)
            except TelegramError as e:
                logger.warning("set_permissions: failed to set permissions for user %s: %s", user_id, e, exc_info=True)
                return False
    logger.warning("set_permissions: Failed to set permissions for user %s after retries", user_id)
    return False
