"""What differs between a private chat and a group."""

from aiogram.types import Message

_GROUP_CHAT_TYPES: frozenset[str] = frozenset({"group", "supergroup"})


def is_group_chat(message: Message) -> bool:
    """Is this message from a group or a supergroup?"""
    chat = getattr(message, "chat", None)
    return getattr(chat, "type", None) in _GROUP_CHAT_TYPES
