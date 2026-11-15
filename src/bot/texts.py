"""Texts of the menu, the help and the hints. HTML: anything from outside is escaped where it is put in."""

from dataclasses import dataclass

from src.config import settings

BATCH_BUTTON_TEXT: str = f"📦 Следующие {settings.LEADS_BATCH_SIZE}"
STATS_BUTTON_TEXT: str = "📊 Статистика"
HELP_BUTTON_TEXT: str = "❓ Помощь"
# The older menu, in the second block of the bottom keyboard.
SEARCH_MENU_BUTTON_TEXT: str = "🔍 Поиск по СПб"
NEW_LEADS_BUTTON_TEXT: str = "📋 Новые лиды"
IN_WORK_BUTTON_TEXT: str = "💼 В работе"
CRM_STATS_BUTTON_TEXT: str = "📊 Статистика и CSV"

NO_ACCESS_TEXT: str = "⛔ Нет доступа"
BATCH_BUSY_TEXT: str = "⏳ Предыдущая пачка ещё отправляется."
BATCH_BUSY_TOAST: str = "Предыдущая пачка ещё отправляется"
BATCH_STARTED_TOAST: str = "⏳ Выдаю лиды…"
ADMIN_ONLY_TEXT: str = "🔒 Эта команда доступна только администраторам бота. Что можно: /help"

START_ADMIN_TEXT: str = (
    "👋 <b>SPb Lead Finder</b>\n"
    "Находит небольшие бизнесы Петербурга без сайта или с неработающим сайтом и выдаёт их пачками.\n"
    f"Нажмите «{BATCH_BUTTON_TEXT}», чтобы получить лиды.\n"
    f"Все команды с примерами: «{HELP_BUTTON_TEXT}»."
)
START_USER_TEXT: str = (
    "👋 <b>SPb Lead Finder</b>\n"
    "Бот подбирает небольшие бизнесы Петербурга, у которых нет сайта.\n"
    "Пользоваться им могут только администраторы бота.\n"
    "Список команд: /help"
)

NOT_UNDERSTOOD_ADMIN_TEXT: str = (
    "Не понял. Вот что можно:\n"
    f"• {BATCH_BUTTON_TEXT}: выдать лиды\n"
    f"• {STATS_BUTTON_TEXT}: сводка по базе\n"
    f"• {HELP_BUTTON_TEXT}: все команды"
)
NOT_UNDERSTOOD_USER_TEXT: str = "Не понял. Вот что можно:\n/start — о боте\n/help — команды"

UNKNOWN_PROVIDER_TEXT: str = (
    "Неизвестный источник: {name}. Пример: <code>/provider_on 2gis_scraper</code>, список: /collect_status"
)

HELP_HIDDEN_TEXT: str = "Остальные команды доступны только администраторам бота."

# ---------- the first message to a lead ----------
# {название}, {ниша}, {предложение} and {подпись} are replaced; the result is shown in <code> and escaped there.
OUTREACH_NO_SITE_TEMPLATE: str = (
    "Здравствуйте! «{название}» есть на картах, но сайта у вас найти не удалось. "
    "В сфере «{ниша}» клиенты часто сначала ищут в интернете цены, адрес и как записаться. "
    "{предложение} Если интересно, расскажу подробнее, а если нет, просто не отвечайте. {подпись}"
)
OUTREACH_DEAD_SITE_TEMPLATE: str = (
    "Здравствуйте! Сайт «{название}» сейчас не открывается. "
    "В сфере «{ниша}» клиенты часто сначала ищут в интернете цены, адрес и как записаться, "
    "и неработающий сайт может их отпугнуть. "
    "{предложение} Если интересно, расскажу подробнее, а если нет, просто не отвечайте. {подпись}"
)
# When OUTREACH_OFFER or OUTREACH_SIGNATURE is empty.
OUTREACH_OFFER_FALLBACK: str = "Я делаю сайты для небольших бизнесов и могу показать, как он мог бы выглядеть у вас."
OUTREACH_SIGNATURE_FALLBACK: str = "Хорошего дня!"
OUTREACH_TITLE: str = "✉️ <b>Первое сообщение</b> (нажмите на текст, чтобы скопировать):"
CONTACTS_TITLE: str = "🔗 <b>Контакты:</b>"
CONTACT_SOURCE_TITLES: dict[str, str] = {
    "2gis": "2ГИС",
    "2gis_scraper": "2ГИС (парсер)",
    "osm": "OSM",
    "google": "Google",
}

# ---------- reminders and the morning plan ----------
REMIND_DAYS: tuple[int, ...] = (1, 3, 7)
REMIND_BUTTON_TEXT: str = "⏰ {days} д"
REMIND_SET_TOAST: str = "Напомню {date}"
REMIND_LINE: str = "⏰ <b>Напомню:</b> {date}"
REMIND_IMPOSSIBLE_TOAST: str = "Напоминание не нужно: лид ответил или отказал"
DIGEST_LATER_DAYS: int = 3
DIGEST_DONE_BUTTON_TEXT: str = "✅ Напомнил"
DIGEST_REJECT_BUTTON_TEXT: str = "❌ Отказ"
DIGEST_LATER_BUTTON_TEXT: str = f"⏰ Отложить на {DIGEST_LATER_DAYS} дня"
DIGEST_DONE_MARK: str = "✅ Напомнили {date}"
DIGEST_REJECT_MARK: str = "❌ Отказ"
DIGEST_LATER_MARK: str = "⏰ Отложено до {date}"
DIGEST_TITLE: str = "☀️ <b>План на {date}</b>"
DIGEST_USEFUL_LINE: str = "• 📦 Полезных лидов в запасе (без сайта и сайт не открывается): <b>{count}</b>"
DIGEST_DUE_LINE: str = "• ⏰ Напоминаний на сегодня: <b>{count}</b>"
DIGEST_STALE_LINE: str = "• ✍️ «Написал» без ответа дольше {days} дн.: <b>{count}</b>"
DIGEST_API_LEFT_LINE: str = "• 🔌 Запросов к API 2ГИС осталось: <b>{left}</b> из {limit}"
DIGEST_API_USED_LINE: str = "• 🔌 Запросов к API 2ГИС израсходовано: <b>{used}</b>, лимит не задан"
DIGEST_FOLLOWUPS_TITLE: str = "<b>Пора напомнить ({count}):</b> ниже по сообщению на каждого."
DIGEST_NO_FOLLOWUPS: str = "Напоминать сегодня некому."
FOLLOWUP_WRITTEN_LINE: str = "✍️ Написали {date}"
FOLLOWUP_DUE_LINE: str = "⏰ Напомнить было {date}"


@dataclass(frozen=True)
class CommandHelp:
    """One command: in the Telegram menu (short) and in /help (with an example)."""

    command: str
    menu: str
    help: str
    example: str
    admin: bool = True


@dataclass(frozen=True)
class HelpSection:
    title: str
    commands: tuple[CommandHelp, ...]
    note: str = ""


HELP_SECTIONS: tuple[HelpSection, ...] = (
    HelpSection(
        "Общее",
        (
            CommandHelp("start", "О боте и главное меню", "О боте и главное меню", "/start", admin=False),
            CommandHelp("help", "Команды с примерами", "Эта справка", "/help", admin=False),
        ),
    ),
    HelpSection(
        "Выдача",
        (
            CommandHelp(
                "next_leads",
                f"Следующие {settings.LEADS_BATCH_SIZE} лидов",
                f"Выдать следующие {settings.LEADS_BATCH_SIZE} лидов, как кнопка «{BATCH_BUTTON_TEXT}»",
                "/next_leads",
            ),
            CommandHelp("rescore", "Пересчитать оценки", "Пересчитать оценку лидов в запасе", "/rescore"),
            CommandHelp(
                "today",
                "План на день",
                "План на день: запас, напоминания и кому пора напомнить, как утренняя сводка",
                "/today",
            ),
        ),
    ),
    HelpSection(
        "Статистика",
        (
            CommandHelp(
                "leads_stats", "Запас и выданные лиды", "Запас, итоги по выданным лидам, выгрузка CSV", "/leads_stats"
            ),
            CommandHelp("collect_status", "Статус сбора", "Расписание, источники и последний сбор", "/collect_status"),
        ),
    ),
    HelpSection(
        "Настройки",
        (
            CommandHelp("collect_now", "Запустить сбор", "Запустить сбор сейчас, в фоне", "/collect_now"),
            CommandHelp("collect_on", "Включить автосбор", "Включить сбор по расписанию", "/collect_on"),
            CommandHelp("collect_off", "Выключить автосбор", "Выключить сбор по расписанию", "/collect_off"),
            CommandHelp(
                "provider_on",
                "Включить источник",
                "Включить источник после блокировок: [источник]",
                "/provider_on 2gis_scraper",
            ),
            CommandHelp("chat_id", "ID этого чата", "ID этого чата, для NOTIFY_CHAT_IDS", "/chat_id"),
        ),
    ),
)
