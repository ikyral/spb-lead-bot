"""
What only the owner of the bot (ADMIN_TG_ID) may do; operators (ADMIN_TG_IDS) may do the rest.

Checked centrally by AdminOnlyMiddleware from the raw text and callback data, before any handler and
its filters run, so a new handler of these commands cannot forget the check.
"""

from aiogram.types import CallbackQuery, Document, Message, TelegramObject

from src.bot.texts import CRM_STATS_BUTTON_TEXT, IN_WORK_BUTTON_TEXT

# Commands without '/', in lower case; any command starting with OWNER_ONLY_COMMAND_PREFIXES too.
OWNER_ONLY_COMMANDS: frozenset[str] = frozenset({"rescore", "provider_on", "team"})
OWNER_ONLY_COMMAND_PREFIXES: tuple[str, ...] = ("collect_",)
# Inline buttons: CSV exports and the collection panel.
OWNER_ONLY_CALLBACK_PREFIXES: tuple[str, ...] = ("export:", "panel:collect", "panel:status")
# Buttons of the bottom keyboard.
OWNER_ONLY_TEXTS: frozenset[str] = frozenset({IN_WORK_BUTTON_TEXT, CRM_STATS_BUTTON_TEXT})


def command_name(text: str | None) -> str | None:
    """'/Rescore@my_bot 30' -> 'rescore'; None if the text is not a command."""
    if not text or not text.startswith("/"):
        return None
    word = text.split(maxsplit=1)[0][1:]
    return word.split("@", 1)[0].lower() or None


def is_owner_only_command(name: str | None) -> bool:
    return name is not None and (name in OWNER_ONLY_COMMANDS or name.startswith(OWNER_ONLY_COMMAND_PREFIXES))


def is_owner_only(event: TelegramObject) -> bool:
    """Does this update ask for something only the owner may do?"""
    if isinstance(event, CallbackQuery):
        data = getattr(event, "data", None)
        return isinstance(data, str) and data.startswith(OWNER_ONLY_CALLBACK_PREFIXES)
    if isinstance(event, Message):
        # Uploading new leads: .json files and .csv harvests alike.
        if isinstance(getattr(event, "document", None), Document):
            return True
        text = event.text if isinstance(getattr(event, "text", None), str) else None
        if text is None:
            return False
        return is_owner_only_command(command_name(text)) or text.strip() in OWNER_ONLY_TEXTS
    return False
