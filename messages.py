"""
Messages file | all messages executes here
"""
import logging
from telegram.ext import ContextTypes
from telegram import Update
from telegram.error import TelegramError

logger = logging.getLogger(__name__)


# Messages

async def send_reply_message(update: Update, text: str):
    try:
        await update.message.reply_text(text)
        logger.debug(
            "messages/send_reply_message: sent reply message to user %s: %s",
            update.effective_user.id, text
        )
    except TelegramError as e:
        logger.warning(
            "messages/send_reply_message: failed to send reply message to user %s: %s",
            update.effective_user.id, e
        )


async def send_effective_chat_message(update: Update, text: str):
    try:
        await update.effective_chat.send_message(text)
        logger.debug(
            "messages/send_effective_chat_message: sent effective chat message to chat %s: %s",
            update.effective_chat.id, text
        )
    except TelegramError as e:
        logger.warning(
            "messages/send_effective_chat_message: failed to send effective chat message to chat %s: %s",
            update.effective_chat.id, e
        )


async def send_default_context_message(context: ContextTypes.DEFAULT_TYPE, text: str, chat_id: str):
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text
        )
        logger.debug(
            "messages/send_default_context_message: sent default context message to chat %s: %s",
            chat_id, text
        )
    except TelegramError as e:
        logger.warning(
            "messages/send_default_context_message: failed to send default context message to chat %s: %s",
            chat_id, e
        )
