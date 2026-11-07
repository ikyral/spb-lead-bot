"""Texts of the menu, the help and the hints. HTML: anything from outside is escaped where it is put in."""

import html
from dataclasses import dataclass

from src.config import settings

BATCH_BUTTON_TEXT: str = f"📦 Следующие {settings.LEADS_BATCH_SIZE}"
ENRICH_BUTTON_TEXT: str = "🔎 Обогащение"
STATS_BUTTON_TEXT: str = "📊 Ниши и статистика"
HELP_BUTTON_TEXT: str = "❓ Помощь"
REQUEST_FILE_BUTTON_TEXT: str = "📄 Получить файл запроса"
MORE_ENRICH_BUTTON_TEXT: str = "🔎 Ещё 30 на обогащение"

NO_ACCESS_TEXT: str = "⛔ Нет доступа"
ADMIN_ONLY_TEXT: str = "🔒 Эта команда доступна только администраторам бота. Что можно: /help"

# Run on the computer with the collector, in the project folder with .venv active.
ENRICH_COMMAND: str = (
    'python collect_cli.py --assisted --enrich "$(ls -t ~/Downloads/enrich_request_*.json | head -1)" --ignore-interval'
)
_ENRICH_COMMAND_HTML: str = f"<code>{html.escape(ENRICH_COMMAND, quote=False)}</code>"

START_ADMIN_TEXT: str = (
    "👋 <b>SPb Lead Finder</b>\n"
    "Находит небольшие бизнесы Петербурга без сайта или с неработающим сайтом и выдаёт их пачками.\n"
    f"Нажмите «{BATCH_BUTTON_TEXT}», чтобы получить лиды, или «{ENRICH_BUTTON_TEXT}», чтобы проверить новые.\n"
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
    f"• {ENRICH_BUTTON_TEXT}: как проверить карточки\n"
    f"• {STATS_BUTTON_TEXT}: сводка по базе\n"
    f"• {HELP_BUTTON_TEXT}: все команды"
)
NOT_UNDERSTOOD_USER_TEXT: str = "Не понял. Вот что можно:\n/start — о боте\n/help — команды"

ENRICH_GUIDE_TEXT: str = (
    "🔎 <b>Обогащение: проверка карточек 2ГИС</b>\n\n"
    f"<b>1.</b> Нажмите «{REQUEST_FILE_BUTTON_TEXT}» ниже. Бот пришлёт <code>enrich_request_….json</code>, "
    "скачайте его на компьютер (в «Загрузки»).\n\n"
    "<b>2.</b> На компьютере откройте терминал в папке проекта, включите <code>.venv</code> "
    "и выполните (нажмите на команду, чтобы скопировать):\n"
    f"{_ENRICH_COMMAND_HTML}\n\n"
    "<b>3.</b> В открывшемся окне браузера пройдите капчу, если она появится. Дождитесь конца: "
    "в папке <code>exports/</code> появится <code>enrich_ГГГГММДД_ЧЧММСС.json</code>.\n\n"
    "<b>4.</b> Отправьте этот файл сюда, как файл. Файл со словом <code>request</code> в имени не подходит.\n\n"
    f"<b>5.</b> Нажмите «{BATCH_BUTTON_TEXT}»: придут карточки лидов.\n\n"
    "Команды бота (/export_unknown) пишутся здесь, в Telegram, а не в терминале."
)
EXPORT_CAPTION_STEPS: str = (
    "<b>Дальше на компьютере</b>, в папке проекта с <code>.venv</code>:\n"
    f"1. Выполните: {_ENRICH_COMMAND_HTML}\n"
    "2. Пройдите капчу в окне браузера.\n"
    "3. Отправьте сюда файл <code>exports/enrich_….json</code> (без слова request)."
)
REQUEST_FILE_HINT: str = (
    "Отправьте файл с результатом: <code>enrich_ГГГГММДД_ЧЧММСС.json</code> (без слова request) "
    "или <code>google_enrich_….json</code>. Он лежит на компьютере в папке <code>exports/</code> проекта."
)

EXPORT_WRONG_NUMBER_TEXT: str = "Число лидов должно быть от 1 до {max}. Пример: <code>/export_unknown 30</code>"
# The niche comes from the admin, escaped by the caller; so is the example.
UNKNOWN_NICHE_TEXT: str = "Неизвестная ниша: {niche}. Пример: <code>/export_unknown 30 {example}</code>"
UNKNOWN_PROVIDER_TEXT: str = (
    "Неизвестный источник: {name}. Пример: <code>/provider_on 2gis_scraper</code>, список: /collect_status"
)

HELP_HIDDEN_TEXT: str = "Остальные команды доступны только администраторам бота."


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
        ),
    ),
    HelpSection(
        "Обогащение",
        (
            CommandHelp(
                "export_unknown",
                "Файл запроса для проверки",
                "Файл запроса для проверки карточек на компьютере: [число] [ниша]",
                "/export_unknown 30 beauty",
            ),
        ),
        note=f"Пошагово: кнопка «{ENRICH_BUTTON_TEXT}». Файл с результатом отправьте боту как файл.",
    ),
    HelpSection(
        "Статистика",
        (
            CommandHelp(
                "leads_stats", "Запас и выданные лиды", "Запас, итоги по выданным лидам, выгрузка CSV", "/leads_stats"
            ),
            CommandHelp("niches", "Доля полезных по нишам", "Доля полезных лидов по нишам", "/niches"),
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
