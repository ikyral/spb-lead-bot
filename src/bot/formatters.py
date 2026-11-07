"""Message and lead card formatters with HTML escaping."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from src.bot.texts import (
    CONTACT_SOURCE_TITLES,
    CONTACTS_TITLE,
    HELP_HIDDEN_TEXT,
    HELP_SECTIONS,
    OUTREACH_DEAD_SITE_TEMPLATE,
    OUTREACH_NO_SITE_TEMPLATE,
    OUTREACH_OFFER_FALLBACK,
    OUTREACH_SIGNATURE_FALLBACK,
    OUTREACH_TITLE,
    REQUEST_FILE_HINT,
    UNKNOWN_NICHE_TEXT,
    HelpSection,
)
from src.config import settings
from src.database.models import (
    CONTACT_CONTACTED,
    CONTACT_NO_ANSWER,
    CONTACT_REJECTED,
    CONTACT_REPLIED,
    STATUS_IN_WORK,
    STATUS_NEW,
    Lead,
)
from src.services.contact_links import (
    KIND_EMAIL,
    KIND_INSTAGRAM,
    KIND_TELEGRAM,
    KIND_VK,
    KIND_WHATSAPP,
    ContactLink,
    lead_contacts,
)
from src.services.lead_file import REQUEST_FILE_ERROR
from src.services.providers.base import TARGET_CATEGORIES, niche_title
from src.services.site_status import (
    NO_SITE_STATUSES,
    SITE_DEAD,
    SITE_FOUND,
    SITE_NO_SITE,
    SITE_NOT_FOUND,
    SITE_SOCIAL_ONLY,
    SITE_UNCERTAIN,
    SITE_UNKNOWN,
    SOURCE_2GIS_CARD,
    SOURCE_MANUAL,
)
from src.services.twogis_links import twogis_card_url, twogis_search_url

if TYPE_CHECKING:  # the scheduler pulls in the database layer, which this module must not import
    from src.database.models import ImportLog, ProviderState
    from src.database.repo import PoolStats
    from src.services.lead_enrich import EnrichStats, UsefulLead
    from src.services.lead_import import ImportResult
    from src.services.niche_priority import NicheStats
    from src.services.scheduler import CollectorStatus, DgisKeyStatus, RunReport

# Telegram's limit for one message.
MAX_MESSAGE_LENGTH: int = 4096
_CARD_FIELD_LIMIT: int = 300
# A lead card as (field length, links shown, with the first message), tried in this order until it fits.
# Normally the first one does; the others are for pathological data.
_CARD_VARIANTS: tuple[tuple[int, int, bool], ...] = (
    (_CARD_FIELD_LIMIT, 8, True),
    (100, 8, True),
    (100, 8, False),
    (100, 3, False),
)
_CONTACT_TITLE_LIMIT: int = 60
_OUTREACH_FIELD_LIMIT: int = 100
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
_MSK = timezone(timedelta(hours=3), "MSK")
_REPORT_FIELD_LIMIT: int = 300
_REPORT_LINKS: int = 5
_NICHE_NAME_LIMIT: int = 60
_PHONE_IN_CARD_LINE: str = "📞 <b>Телефон:</b> смотри в карточке"

# Short names for the statistics, in the order of handing out. They say no more than the cards do.
_SITE_STATUS_TITLES: dict[str, str] = {
    SITE_NO_SITE: "Сайта нет по данным источника или вручную",
    SITE_DEAD: "Сайт не работает",
    SITE_SOCIAL_ONLY: "Только соцсеть",
    SITE_NOT_FOUND: "Сайт не найден в поиске",
    SITE_UNCERTAIN: "Возможно, сайт есть",
    SITE_UNKNOWN: "Статус неизвестен",
}
# Enough to see what prevails; a list of 18 niches would hide the rest of the message.
_NICHES_SHOWN: int = 8
_CONTACT_STATUS_TITLES: dict[str, str] = {
    CONTACT_CONTACTED: "✍️ Написал",
    CONTACT_REPLIED: "💬 Ответили",
    CONTACT_REJECTED: "🗑 Отказ",
    CONTACT_NO_ANSWER: "🔇 Нет ответа",
}
_CARD_LINK_TITLES: dict[str, str] = {"2gis.": "Карточка 2ГИС", "openstreetmap.": "Карточка OpenStreetMap"}
_RUN_STATUS_TITLES: dict[str, str] = {
    "running": "выполняется",
    "ok": "успешно",
    "partial": "частично, были ошибки",
    "failed": "ошибка",
    "interrupted": "прерван",
    "daily_limit": "пропущен, суточный лимит запросов",
}
_TRIGGER_TITLES: dict[str, str] = {
    "schedule": "по расписанию",
    "manual": "вручную",
    "batch": "при выдаче пачки",
    "low_pool": "мало лидов в запасе",
}


def format_user_label(username: str | None, full_name: str) -> str:
    """How to name a person in a chat: @username if there is one, otherwise the name."""
    return f"@{username}" if username else full_name


def format_msk_time(value: datetime | None) -> str:
    """Format a timestamp as Moscow time for messages."""
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(_MSK).strftime("%d.%m.%Y %H:%M МСК")


def _site_verdict_line(lead: Lead, field_limit: int) -> str | None:
    """
    A line that says exactly how much is known: 'no site' only where a source or a person says so.

    None for a status with a line of its own in each form of a card.
    """
    url = html.escape((lead.site_url or lead.raw_website or "")[:field_limit])
    if lead.site_status == SITE_NO_SITE:
        if lead.site_source == SOURCE_MANUAL:
            return "✅ Сайта нет (проверено вручную)"
        if lead.site_source == SOURCE_2GIS_CARD:
            return "🌐 В 2ГИС сайт не указан"
        return "🌐 Сайта нет (по данным источника)"
    if lead.site_status == SITE_NOT_FOUND:
        return "🌐 Сайт не найден в поиске"
    if lead.site_status == SITE_UNCERTAIN:
        return f"🌐 Возможно, сайт: {url}, проверьте" if url else "🌐 Возможно, сайт есть, проверьте"
    if lead.site_status == SITE_UNKNOWN:
        return "🌐 Статус неизвестен"
    if lead.site_status == SITE_FOUND:
        return f"🌐 Сайт есть ({url})" if url else "🌐 Сайт есть"
    return None


def site_verdict_text(has_site: bool) -> str:
    """What a person sees after giving a verdict about the site."""
    return "Сайт есть: лид больше не будет выдаваться" if has_site else "Сайта нет: отмечено"


def format_lead_card(lead: Lead) -> str:
    """Format business lead details as an HTML message for Telegram."""
    category_title = TARGET_CATEGORIES.get(lead.category, {}).get("title", lead.category)
    safe_name = html.escape(lead.name)
    safe_category = html.escape(category_title)

    site_line = _site_verdict_line(lead, _CARD_FIELD_LIMIT)
    if site_line is None:
        if lead.site_status == SITE_DEAD:
            safe_site = html.escape(lead.raw_website or "не указан")
            status_line = f"⚠️ Мертвый сайт / ошибка домена ({safe_site})"
        elif lead.site_status == SITE_SOCIAL_ONLY:
            status_line = "📱 Только соцсеть вместо сайта"
        else:
            safe_site = html.escape(lead.raw_website or "")
            status_line = f"🌐 Есть сайт ({safe_site})"
        site_line = f"🌐 <b>Статус сайта:</b> {status_line}"

    lines = [f"🏢 <b>{safe_name}</b> | {safe_category}", f"🔥 {lead.score}/100", site_line]

    if lead.district:
        lines.append(f"📍 <b>Район:</b> {html.escape(lead.district)}")
    if lead.address:
        lines.append(f"📫 <b>Адрес:</b> {html.escape(lead.address)}")
    if lead.phone:
        lines.append(f"📞 <b>Телефон:</b> {html.escape(lead.phone)}")
    elif twogis_card_url(lead.source_url):
        lines.append(_PHONE_IN_CARD_LINE)
    if lead.email:
        lines.append(f"✉️ <b>Email:</b> {html.escape(lead.email)}")
    if lead.socials:
        lines.append(f"🔗 <b>Соцсети:</b> {html.escape(lead.socials)}")

    return "\n".join(lines)


def _site_line(lead: Lead, field_limit: int) -> str:
    verdict = _site_verdict_line(lead, field_limit)
    if verdict is not None:
        return verdict
    site = html.escape((lead.raw_website or "")[:field_limit])
    if lead.site_status == SITE_DEAD:
        return f"🌐 <b>Сайт:</b> ⚠️ не работает ({site or 'адрес не указан'})"
    if lead.site_status == SITE_SOCIAL_ONLY:
        return f"🌐 <b>Сайт:</b> 📱 только соцсеть{f' ({site})' if site else ''}"
    return f"🌐 <b>Сайт:</b> {site}"


def _card_link_line(lead: Lead) -> str | None:
    if lead.source_url and lead.source_url.startswith(("http://", "https://")):
        # By the link, not lead.source: when sources are merged the card may come from another one.
        title = next((text for host, text in _CARD_LINK_TITLES.items() if host in lead.source_url), "Карточка")
        return f'🗺 <a href="{html.escape(lead.source_url, quote=True)}">{title}</a>'
    if lead.map_url and lead.map_url.startswith(("http://", "https://")):
        return f'🗺 <a href="{html.escape(lead.map_url, quote=True)}">На карте</a>'
    return None


def _issued_card(lead: Lead, field_limit: int, max_links: int, with_outreach: bool) -> str:
    def field(value: str) -> str:
        return html.escape(value[:field_limit])

    category = lead.rubric or str(TARGET_CATEGORIES.get(lead.category, {}).get("title", lead.category))
    # A link to 2GIS is on the button under the message, see get_issued_lead_keyboard.
    on_button = twogis_card_url(lead.source_url) is not None
    lines = [f"🏢 <b>{field(lead.name)}</b>", f"🏷 <b>Рубрика:</b> {field(category)}"]
    if lead.address:
        lines.append(f"📫 <b>Адрес:</b> {field(lead.address)}")
    if lead.phone:
        lines.append(f"📞 <b>Телефон:</b> {field(lead.phone)}")
    elif on_button:
        lines.append(_PHONE_IN_CARD_LINE)
    lines.append(_site_line(lead, field_limit))
    contacts = format_contacts_line(lead_contacts(lead.contact_links, lead.email, lead.socials)[:max_links])
    if contacts:
        lines.append(contacts)
    link = None if on_button else _card_link_line(lead)
    if link:
        lines.append(link)
    status = _CONTACT_STATUS_TITLES.get(lead.contact_status or "", "📤 Выдан, ждёт действия")
    if lead.contact_status and lead.contact_status_by:
        status += f" — {field(lead.contact_status_by)}"
    lines.append(f"📌 <b>Статус:</b> {status}")
    outreach = outreach_text(lead) if with_outreach else None
    if outreach:
        lines += ["", OUTREACH_TITLE, f"<code>{html.escape(outreach)}</code>"]
    return "\n".join(lines)


def format_issued_lead(lead: Lead) -> str:
    """Format a handed-out lead: everything needed to contact the organization, within Telegram's limit."""
    text = ""
    # Shorter fields first, then fewer links, then no ready message: the contacts matter more than the text.
    for field_limit, max_links, with_outreach in _CARD_VARIANTS:
        text = _issued_card(lead, field_limit, max_links, with_outreach)
        if len(text) <= MAX_MESSAGE_LENGTH:
            break
    return text


def _contact_title(link: ContactLink) -> str:
    """What the owner reads on a link: the address, the number or the nickname; the host for anything else."""
    tail = link.url.rstrip("/").rsplit("/", 1)[-1]
    if link.kind == KIND_EMAIL:
        return link.url.removeprefix("mailto:")
    if link.kind == KIND_WHATSAPP:
        return f"WhatsApp +{tail}"
    if link.kind == KIND_TELEGRAM:
        return f"Telegram @{tail}"
    if link.kind == KIND_INSTAGRAM:
        return f"Instagram @{tail}"
    if link.kind == KIND_VK:
        return f"ВКонтакте {tail}"
    return link.url.removeprefix("https://")


def format_contacts_line(links: list[ContactLink]) -> str | None:
    """The contacts as links: mailto:, t.me, wa.me, vk.com. Each source in brackets; nothing for no contacts."""
    parts = []
    for link in links:
        title = html.escape(_contact_title(link)[:_CONTACT_TITLE_LIMIT])
        sources = ", ".join(CONTACT_SOURCE_TITLES[source] for source in link.sources)
        suffix = f" ({sources})" if sources else ""
        parts.append(f'<a href="{html.escape(link.url, quote=True)}">{title}</a>{suffix}')
    return f"{CONTACTS_TITLE} " + " · ".join(parts) if parts else None


def _niche_for_text(lead: Lead) -> str:
    """The niche in words, without the emoji of its title: the rubric of 2GIS if known."""
    title = lead.rubric or niche_title(lead.category)
    return "".join(char for char in title if char.isalnum() or char in " ,-«»").strip()[:_OUTREACH_FIELD_LIMIT]


def outreach_text(lead: Lead) -> str | None:
    """A ready first message for a lead without a site or with a site that does not open; None for the rest."""
    if lead.site_status == SITE_DEAD:
        template = OUTREACH_DEAD_SITE_TEMPLATE
    elif lead.site_status in NO_SITE_STATUSES:
        template = OUTREACH_NO_SITE_TEMPLATE
    else:
        return None
    values = {
        "название": " ".join(lead.name.split())[:_OUTREACH_FIELD_LIMIT],
        "ниша": _niche_for_text(lead) or "вашей",
        "предложение": settings.OUTREACH_OFFER or OUTREACH_OFFER_FALLBACK,
        "подпись": settings.OUTREACH_SIGNATURE or OUTREACH_SIGNATURE_FALLBACK,
    }
    # One pass over the template: braces in a name or in the settings stay text, an unknown placeholder stays too.
    text = _PLACEHOLDER_RE.sub(lambda match: values.get(match.group(1), match.group(0)), template)
    return " ".join(text.split())


def _pool_stats_lines(stats: PoolStats) -> list[str]:
    """The pool by status of the site and by niche, how many wait for a check, how often sources were wrong."""
    lines = ["", "<b>Запас по статусу сайта:</b>"]
    lines += [
        f"• {title}: <b>{stats.by_site[status]}</b>"
        for status, title in _SITE_STATUS_TITLES.items()
        if stats.by_site.get(status)
    ]
    if not stats.by_site:
        lines.append("• пусто")
    lines.append(f"⏳ Ждут проверки сайта: <b>{stats.waiting_for_check}</b>")
    lines.append(f"🌐 Вердиктов «Сайт есть» вручную: <b>{stats.manual_site_found}</b>")
    if stats.chains:
        lines.append(f"🔗 Сетей не выдаётся: <b>{stats.chains}</b>")
    if stats.by_niche:
        lines += ["", "<b>Запас по нишам:</b>"]
        niches = sorted(stats.by_niche.items(), key=lambda item: (-item[1], item[0]))
        for key, count in niches[:_NICHES_SHOWN]:
            title = str(TARGET_CATEGORIES.get(key, {}).get("title", key))
            lines.append(f"• {html.escape(title)}: <b>{count}</b>")
        if len(niches) > _NICHES_SHOWN:
            lines.append(f"• другие: <b>{sum(count for _, count in niches[_NICHES_SHOWN:])}</b>")
    return lines


def format_funnel_stats(
    counts: dict[str, int],
    contact_counts: dict[str, int],
    issued_total: int,
    pool_stats: PoolStats | None = None,
) -> str:
    """Format the pool size and what happened to the handed-out leads."""
    waiting = max(0, issued_total - sum(contact_counts.values()))
    # Leads with a known site and chains are new too, but they will never be handed out.
    pool_size = sum(pool_stats.by_site.values()) if pool_stats is not None else counts.get(STATUS_NEW, 0)
    lines = [
        "📊 <b>Лиды:</b>\n",
        f"• 📈 Всего: <b>{sum(counts.values())}</b>",
        f"• 📦 Новых в запасе: <b>{pool_size}</b>",
        f"• 📤 Выдано всего: <b>{issued_total}</b>",
        f"• ⏳ Выдано, ждут действия: <b>{waiting}</b>",
        f"• ✍️ Написал: <b>{contact_counts.get(CONTACT_CONTACTED, 0)}</b>",
        f"• 💬 Ответили: <b>{contact_counts.get(CONTACT_REPLIED, 0)}</b>",
        f"• 🗑 Отказ: <b>{contact_counts.get(CONTACT_REJECTED, 0)}</b>",
        f"• 🔇 Нет ответа: <b>{contact_counts.get(CONTACT_NO_ANSWER, 0)}</b>",
    ]
    if counts.get(STATUS_IN_WORK):
        lines.append(f"• 💼 В работе: <b>{counts[STATUS_IN_WORK]}</b>")
    if pool_stats is not None:
        lines += _pool_stats_lines(pool_stats)
    return "\n".join(lines)


def format_collection_report(report: RunReport) -> str:
    """Format the outcome of a collection run."""
    result = report.result
    status = _RUN_STATUS_TITLES.get(report.status, report.status)
    lines = [
        f"🏁 <b>Сбор лидов завершён</b> ({status})\n",
        f"• Найдено: <b>{result.found}</b>",
        f"• Подходят: <b>{result.qualified}</b>",
        f"• Новых сохранено: <b>{result.saved_new}</b>",
        f"• Дубликатов пропущено: <b>{result.duplicates}</b>",
        f"• Ошибок: <b>{result.errors}</b>",
        f"• Запросов к API: <b>{result.requests}</b>",
        f"• Длительность: <b>{report.duration_seconds:.0f} с</b>",
        f"\n📦 В запасе: <b>{report.pool_size}</b>",
    ]
    if result.last_error:
        lines.append(f"\n⚠️ Последняя ошибка: {html.escape(result.last_error)}")
    lines += [f"⏭ {html.escape(name)} пропущен: {html.escape(why)}" for name, why in result.skipped.items()]
    return "\n".join(lines)


def format_import_messages(result: ImportResult) -> list[str]:
    """All the messages about an imported file: a file with results of reading cards may need several."""
    if result.ok and result.enrichment is not None:
        name = html.escape(result.file_name)
        return _enrichment_messages(name, result.total, result.enrichment, result.pool_size)
    return [format_import_report(result)]


def format_import_report(result: ImportResult) -> str:
    """Format the outcome of an imported file. The file name comes from outside and is escaped."""
    name = html.escape(result.file_name)
    if not result.ok:
        text = f"⚠️ <b>Импорт не выполнен</b>: {name}\n\nПричина: {html.escape(result.error or 'неизвестна')}"
        return f"{text}\n\n{REQUEST_FILE_HINT}" if result.error == REQUEST_FILE_ERROR else text
    if result.enrichment is not None:
        return _enrichment_messages(name, result.total, result.enrichment, result.pool_size)[0]
    lines = [
        f"📥 <b>Импорт завершён</b>: {name}\n",
        f"• В файле: <b>{result.total}</b>",
        f"• Новых: <b>{result.saved_new}</b>",
        f"• Дубликатов: <b>{result.duplicates}</b>",
        f"• Отклонено: <b>{result.rejected_total}</b>",
    ]
    lines += [f"   – {html.escape(reason)}: {count}" for reason, count in result.rejected.most_common()]
    lines.append(f"\n📦 В запасе: <b>{result.pool_size}</b>")
    return "\n".join(lines)


def split_messages(blocks: list[str], separator: str = "\n\n") -> list[str]:
    """Join blocks of text into messages within Telegram's limit, never cutting a block with its tags in two."""
    messages: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}{separator}{block}" if current else block
        if len(candidate) <= MAX_MESSAGE_LENGTH:
            current = candidate
        else:
            if current:
                messages.append(current)
            current = block
    if current:
        messages.append(current)
    return messages


def _useful_lead_block(lead: UsefulLead) -> str:
    def field(value: str) -> str:
        return html.escape(value[:_REPORT_FIELD_LIMIT])

    lines = [f"🏢 <b>{field(lead.name)}</b>", f"🏷 {field(niche_title(lead.category))}"]
    if lead.address:
        lines.append(f"📫 {field(lead.address)}")
    if lead.phone:
        lines.append(f"📞 {field(lead.phone)}")
    if lead.site_status == SITE_SOCIAL_ONLY:
        lines.append("📱 только соцсеть вместо сайта")
    if lead.site_status == SITE_DEAD and lead.website:
        lines.append(f"🌐 {field(lead.website)}")
    contacts = format_contacts_line(lead.contacts[:_REPORT_LINKS])
    if contacts:
        lines.append(contacts)
    url = twogis_card_url(lead.source_url) or twogis_search_url(lead.name, lead.address, settings.SCRAPER_CITY_SLUG)
    title = "Открыть в 2ГИС" if twogis_card_url(lead.source_url) else "Найти в 2ГИС"
    lines.append(f'🗺 <a href="{html.escape(url, quote=True)}">{title}</a>')
    return "\n".join(lines)


def _useful_leads_blocks(useful: list[UsefulLead]) -> list[str]:
    without_site = [lead for lead in useful if lead.site_status != SITE_DEAD]
    dead = [lead for lead in useful if lead.site_status == SITE_DEAD]
    blocks: list[str] = []
    for title, leads in (("Сайта нет", without_site), ("Сайт не открывается", dead)):
        if leads:
            blocks.append(f"<b>{title}: {len(leads)}</b>")
            blocks += [_useful_lead_block(lead) for lead in leads]
    return blocks or ["Полезных лидов в этом файле нет"]


def _enrichment_messages(name: str, total: int, stats: EnrichStats, pool_size: int) -> list[str]:
    title = "Поиск в Google обработан" if stats.from_google else "Карточки 2ГИС обработаны"
    lines = [
        f"🔎 <b>{title}</b>: {name}\n",
        f"• В файле: <b>{total}</b>",
        f"• Обновлено лидов: <b>{stats.updated}</b>",
        f"• Сайтов найдено: <b>{stats.sites_found}</b> (рабочих: {stats.sites_working}, "
        f"не открываются: {stats.sites_dead}, соцсеть или агрегатор: {stats.sites_social})",
    ]
    if stats.from_google:
        lines.append(f"• Сайт и соцсети в поиске не найдены: <b>{stats.not_found}</b>")
    if stats.no_site or not stats.from_google:
        lines.append(f"• Без сайта в карточке: <b>{stats.no_site}</b>")
    lines += [
        f"• Не распознано: <b>{stats.not_recognized}</b>",
        f"• Проигнорировано: <b>{stats.ignored_total}</b>",
    ]
    lines += [f"   – {html.escape(reason)}: {count}" for reason, count in stats.ignored.most_common()]
    lines.append(f"\n📦 В запасе: <b>{pool_size}</b>")
    return split_messages(["\n".join(lines), *_useful_leads_blocks(stats.useful)])


def format_unknown_niche(text: str, niches: list[str]) -> list[str]:
    """What to say about a niche that does not exist, with the list of those that do."""
    example = html.escape(niches[0] if niches else next(iter(TARGET_CATEGORIES)))
    header = UNKNOWN_NICHE_TEXT.format(niche=html.escape(text[:_NICHE_NAME_LIMIT]), example=example)
    header += "\n\n<b>Ниши:</b>"
    lines = [f"• <code>{html.escape(niche)}</code> — {html.escape(niche_title(niche))}" for niche in niches]
    return split_messages([header, *lines], separator="\n")


def format_help(is_admin: bool, sections: tuple[HelpSection, ...] = HELP_SECTIONS) -> list[str]:
    """/help in as many messages as it takes. Admin commands are marked 🔒 and shown to admins only."""
    blocks = ["❓ <b>Команды бота</b>"]
    hidden = False
    for section in sections:
        commands = [command for command in section.commands if is_admin or not command.admin]
        hidden = hidden or len(commands) < len(section.commands)
        if not commands:
            continue
        blocks.append(f"\n<b>{html.escape(section.title)}</b>")
        for command in commands:
            mark = "🔒 " if command.admin else ""
            blocks.append(
                f"{mark}/{command.command} — {html.escape(command.help)}\n"
                f"   Пример: <code>{html.escape(command.example)}</code>"
            )
        if section.note:
            blocks.append(html.escape(section.note))
    if hidden:
        blocks.append(f"\n{HELP_HIDDEN_TEXT}")
    return split_messages(blocks, separator="\n")


def format_niche_table(stats: list[NicheStats]) -> list[str]:
    """The table of /niches, in as many messages as it takes. Each message is a table of its own."""
    if not stats:
        return ["Лидов пока нет, считать нечего."]
    legend = (
        "📊 <b>Ниши по доле полезных лидов</b>\n"
        "Полезные: без сайта и сайт не открывается. Доля сглажена: (полезные + 1) / (проверено + 2), "
        "поэтому у непроверенной ниши 50%. Сайта нет считается вместе с «только соцсеть»."
    )
    header = f"{'доля':>4} {'пров':>4} {'нет':>3} {'мёртв':>5} {'есть':>4} {'unkn':>4}  ниша"
    rows = [
        f"{round(item.share * 100):>3}% {item.checked:>4} {item.no_site:>3} {item.dead:>5} "
        f"{item.has_site:>4} {item.unknown:>4}  {html.escape(niche_title(item.category)[:_NICHE_NAME_LIMIT])}"
        for item in stats
    ]
    wrapper = len("<pre></pre>") + len(header) + 1
    tables: list[str] = []
    current: list[str] = []
    for row in rows:
        if current and wrapper + sum(len(line) + 1 for line in current) + len(row) > MAX_MESSAGE_LENGTH:
            tables.append(_niche_table(header, current))
            current = []
        current.append(row)
    tables.append(_niche_table(header, current))
    return split_messages([legend, *tables])


def _niche_table(header: str, rows: list[str]) -> str:
    return "<pre>" + "\n".join([header, *rows]) + "</pre>"


def _import_lines(last: ImportLog | None, imported_total: int) -> list[str]:
    lines = [f"\n<b>Импорт файлов</b>: всего пришло лидов <b>{imported_total}</b>"]
    if last is None:
        lines.append("Файлы ещё не импортировались.")
        return lines
    lines.append(f"Последний: {format_msk_time(last.finished_at)}, {html.escape(last.file_name)}")
    if last.ok:
        lines.append(
            f"В файле: <b>{last.total}</b>, новых: <b>{last.saved_new}</b>, "
            f"дубликатов: <b>{last.duplicates}</b>, отклонено: <b>{last.rejected}</b>"
        )
    else:
        lines.append(f"⚠️ Не выполнен: {html.escape(last.error or 'причина неизвестна')}")
    return lines


def _stock_lines(status: CollectorStatus) -> list[str]:
    """How many leads are left, of what kind, and for how long they will last."""
    with_contacts = status.pool_with_contacts
    lines = [
        f"📦 В запасе: <b>{status.pool_size}</b> "
        f"(с контактами: <b>{with_contacts}</b>, без контактов: <b>{status.pool_size - with_contacts}</b>)"
    ]
    if status.batch_size > 0:
        days = status.pool_size // status.batch_size
        if status.pool_size == 0:
            lines.append("Запас пуст.")
        elif days == 0:
            lines.append(f"Хватит меньше чем на день при выдаче по {status.batch_size} лидов в день.")
        else:
            lines.append(f"Хватит примерно на <b>{days}</b> дн. при выдаче по {status.batch_size} лидов в день.")
    return lines


def _dgis_key_lines(key: DgisKeyStatus) -> list[str]:
    """What is left of the API key: requests and days."""
    if key.limit:
        left = max(0, key.limit - key.used)
        requests = f"израсходовано <b>{key.used}</b> из {key.limit}, осталось <b>{left}</b> (резерв {key.reserve})"
    else:
        requests = f"израсходовано <b>{key.used}</b>, лимит не задан"
    lines = [f"\n<b>API 2ГИС</b>: {requests}"]
    if key.expires_on is not None and key.days_left is not None:
        shown = key.expires_on.strftime("%d.%m.%Y")
        if key.days_left < 0:
            lines.append(f"⌛ Срок ключа закончился {shown}")
        else:
            lines.append(f"Ключ действует до {shown}, осталось дней: <b>{key.days_left}</b>")
    else:
        lines.append("Срок ключа неизвестен: задайте DGIS_KEY_CREATED_AT")
    lines.append(f"Контакты: {'запрашиваются' if key.request_contacts else 'не запрашиваются, смотри в карточке'}")
    return lines


def _site_stock_lines(status: CollectorStatus) -> list[str]:
    return _pool_stats_lines(status.pool_stats) if status.pool_stats is not None else []


def _provider_line(name: str, state: ProviderState | None, now: datetime) -> str:
    safe_name = html.escape(name)
    if state is None:
        return f"✅ {safe_name}"
    reason = html.escape(state.last_block_reason or "")
    if state.disabled_at is not None:
        cause = "после блокировок " if state.consecutive_blocks else ""
        return f"⛔ {safe_name}: отключён {cause}({reason}). Включить: /provider_on {safe_name}"
    if state.blocked_until is not None and now < state.blocked_until:
        return f"⏸ {safe_name}: пауза до {format_msk_time(state.blocked_until)} ({reason})"
    return f"✅ {safe_name}"


def format_collector_status(status: CollectorStatus) -> str:
    """Format the state of automatic collection."""
    state = status.state
    now = datetime.now(UTC)
    if status.external_mode:
        return "\n".join(
            [
                "🤖 <b>Сбор лидов</b>\n",
                "Режим: <b>внешний сборщик</b>. Этот сервер сам не собирает, лиды приходят файлами.",
                *_stock_lines(status),
                *_site_stock_lines(status),
                *(_dgis_key_lines(status.dgis_key) if status.dgis_key else []),
                *_import_lines(status.last_import, status.imported_total),
            ]
        )

    lines = [
        "🤖 <b>Автосбор лидов</b>\n",
        f"Автозапуск: <b>{'включён' if status.auto_enabled else 'выключен'}</b>",
        f"Расписание: {html.escape(status.schedule_text)}",
        f"Следующий запуск: <b>{format_msk_time(status.next_run_at)}</b>",
        "\n<b>Источники:</b>",
    ]
    lines += [_provider_line(name, status.provider_states.get(name), now) for name in status.active_providers]
    if not status.active_providers:
        lines.append("нет доступных")
    lines += [
        f"⚠️ {html.escape(name)} отключён: {html.escape(reason)}" for name, reason in status.disabled_providers.items()
    ]
    lines.append("")
    lines += _stock_lines(status)
    lines += _site_stock_lines(status)
    lines.append(f"Запросов за сутки: {status.requests_today} из {status.max_requests_per_day}")
    if status.dgis_key:
        lines += _dgis_key_lines(status.dgis_key)
    if status.is_running:
        lines.append("\n⏳ <b>Сбор выполняется прямо сейчас</b>")

    if state.last_started_at is None:
        lines.append("\nСбор ещё не запускался.")
        lines += _import_lines(status.last_import, status.imported_total)
        return "\n".join(lines)

    run_status = _RUN_STATUS_TITLES.get(state.last_status or "", state.last_status or "—")
    trigger = _TRIGGER_TITLES.get(state.last_trigger or "", state.last_trigger or "—")
    lines += [
        f"\n<b>Последний запуск</b> ({trigger}): {format_msk_time(state.last_started_at)}",
        f"Результат: <b>{html.escape(run_status)}</b>",
        f"Найдено: <b>{state.found}</b>, подходят: <b>{state.qualified}</b>, новых: <b>{state.saved_new}</b>",
        f"Дубликатов: <b>{state.duplicates}</b>, ошибок: <b>{state.errors}</b>",
    ]
    if state.last_error:
        lines.append(f"⚠️ Последняя ошибка: {html.escape(state.last_error)}")
    lines += _import_lines(status.last_import, status.imported_total)
    return "\n".join(lines)


def generate_pitch_text(lead: Lead) -> str:
    """Generate ready personalized outreach text for the business owner."""
    location_str = f"в районе {lead.district}" if lead.district else "в Санкт-Петербурге"
    if lead.site_status == SITE_DEAD:
        website = lead.raw_website or "указанный на картах"
        return (
            f"Здравствуйте! Обратил внимание на карточку вашей компании «{lead.name}» на картах СПб. "
            f"При переходе сайт {website} выдает ошибку и не открывается. "
            "Из-за этого вы теряете заявки и клиентов, которые приходят из поиска и геосервисов.\n\n"
            "Я веб-разработчик из Санкт-Петербурга. Могу оперативно помочь восстановить работу текущего сайта "
            "или разработать новый быстрый и удобный сайт для вашего бизнеса. "
            "Подскажите, актуален ли для вас этот вопрос?"
        )

    if lead.site_status == SITE_SOCIAL_ONLY:
        return (
            f"Здравствуйте! У компании «{lead.name}» отличная карточка {location_str}, "
            "но вместо полноценного сайта указана только соцсеть. "
            "Из-за отсутствия сайта теряется целевой поисковый трафик из Яндекса и Google, "
            "где клиенты ищут услуги напрямую.\n\n"
            f"Я создаю сайты для локального бизнеса в СПб и могу подготовить бесплатный демо-прототип "
            f"современного сайта для «{lead.name}», чтобы вы оценили результат. "
            "Было бы интересно взглянуть?"
        )

    if lead.site_status != SITE_NO_SITE or lead.site_source != SOURCE_MANUAL:
        if lead.site_status == SITE_NO_SITE:
            # A source only lists no site: that can be said to the owner, but not more than that.
            return (
                f"Здравствуйте! Нашел компанию «{lead.name}» на картах Санкт-Петербурга: в карточке не указан сайт. "
                "Я разрабатываю сайты для локального бизнеса: они приводят клиентов из поиска Яндекса и Google "
                "и повышают доверие к компании.\n\n"
                f"Если сайта у «{lead.name}» пока нет или он устарел, могу предложить варианты под ваши задачи. "
                "Удобно обсудить?"
            )
        # Nobody has confirmed anything about the site: the text must not claim there is none.
        return (
            f"Здравствуйте! Нашел компанию «{lead.name}» на картах Санкт-Петербурга. "
            "Я разрабатываю сайты для локального бизнеса: они приводят клиентов из поиска Яндекса и Google "
            "и повышают доверие к компании.\n\n"
            f"Если сайта у «{lead.name}» пока нет или он устарел, могу предложить варианты под ваши задачи. "
            "Удобно обсудить?"
        )

    return (
        f"Здравствуйте! Нашел компанию «{lead.name}» на картах Санкт-Петербурга. "
        "У вас отличный бизнес, но сейчас нет собственного сайта, из-за чего часть клиентов "
        "уходит к конкурентам из поисковой выдачи Яндекса и Google.\n\n"
        f"Предлагаю разработать современный сайт под ключ для «{lead.name}», "
        "который привлечет целевых клиентов из поиска и повысит доверие к компании. "
        "Удобно обсудить варианты реализации?"
    )
