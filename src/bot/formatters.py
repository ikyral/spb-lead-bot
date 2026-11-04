"""Message and lead card formatters with HTML escaping."""

import html
from src.database.models import Lead
from src.services.providers.base import TARGET_CATEGORIES


def format_lead_card(lead: Lead) -> str:
    """Format business lead details as an HTML message for Telegram."""
    category_title = TARGET_CATEGORIES.get(lead.category, {}).get("title", lead.category)
    safe_name = html.escape(lead.name)
    safe_category = html.escape(category_title)

    if lead.site_status == "dead_site":
        safe_site = html.escape(lead.raw_website or "не указан")
        status_line = f"⚠️ Мертвый сайт / ошибка домена ({safe_site})"
    elif lead.site_status == "social_only":
        status_line = "📱 Только соцсеть вместо сайта"
    elif lead.site_status == "no_site":
        status_line = "❌ Нет сайта"
    else:
        safe_site = html.escape(lead.raw_website or "")
        status_line = f"🌐 Есть сайт ({safe_site})"

    lines = [
        f"🏢 <b>{safe_name}</b> | {safe_category}",
        f"🔥 {lead.score}/100",
        f"🌐 <b>Статус сайта:</b> {status_line}",
    ]

    if lead.district:
        lines.append(f"📍 <b>Район:</b> {html.escape(lead.district)}")
    if lead.address:
        lines.append(f"📫 <b>Адрес:</b> {html.escape(lead.address)}")
    if lead.phone:
        lines.append(f"📞 <b>Телефон:</b> {html.escape(lead.phone)}")
    if lead.socials:
        lines.append(f"🔗 <b>Соцсети:</b> {html.escape(lead.socials)}")

    return "\n".join(lines)


def generate_pitch_text(lead: Lead) -> str:
    """Generate ready personalized outreach text for the business owner."""
    district_str = lead.district or "Санкт-Петербурге"
    if lead.site_status == "dead_site":
        website = lead.raw_website or "указанный на картах"
        return (
            f"Здравствуйте! Обратил внимание на карточку вашей компании «{lead.name}» на картах СПб. "
            f"При переходе сайт {website} выдает ошибку и не открывается. "
            "Из-за этого вы теряете заявки и клиентов, которые приходят из поиска и геосервисов.\n\n"
            "Я веб-разработчик из Санкт-Петербурга. Могу оперативно помочь восстановить работу текущего сайта "
            "или разработать новый быстрый и удобный сайт для вашего бизнеса. "
            "Подскажите, актуален ли для вас этот вопрос?"
        )

    if lead.site_status == "social_only":
        return (
            f"Здравствуйте! У компании «{lead.name}» отличная карточка в районе {district_str}, "
            "но вместо полноценного сайта указана только соцсеть. "
            "Из-за отсутствия сайта теряется целевой поисковый трафик из Яндекса и Google, "
            "где клиенты ищут услуги напрямую.\n\n"
            f"Я создаю сайты для локального бизнеса в СПб и могу подготовить бесплатный демо-прототип "
            f"современного сайта для «{lead.name}», чтобы вы оценили результат. "
            "Было бы интересно взглянуть?"
        )

    return (
        f"Здравствуйте! Нашел компанию «{lead.name}» на картах Санкт-Петербурга. "
        "У вас отличный бизнес, но сейчас нет собственного сайта, из-за чего часть клиентов "
        "уходит к конкурентам из поисковой выдачи Яндекса и Google.\n\n"
        f"Предлагаю разработать современный сайт под ключ для «{lead.name}», "
        "который привлечет целевых клиентов из поиска и повысит доверие к компании. "
        "Удобно обсудить варианты реализации?"
    )
