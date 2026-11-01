import discord

from utils import branding

COLORS = {
    "success": 0x57F287,
    "error": 0xED4245,
    "warn": 0xFEE75C,
    "info": 0x5865F2,
    "mod": 0xEB459E,
}


def make(title=None, description=None, kind="info", footer=None, section=None) -> discord.Embed:
    """Crée un embed aux couleurs du serveur (nom + logo dans le pied de page)."""
    embed = discord.Embed(
        title=title,
        description=description,
        color=COLORS[kind],
        timestamp=discord.utils.utcnow(),
    )
    text = " • ".join(part for part in (footer, branding.name()) if part)
    icon = branding.logo(section)
    if text or icon:
        embed.set_footer(text=text or "\u200b", icon_url=icon)
    return embed


def success(description, title="Succès") -> discord.Embed:
    return make(f"✅ {title}", description, "success")


def error(description, title="Erreur") -> discord.Embed:
    return make(f"❌ {title}", description, "error")


def info(description, title="Information") -> discord.Embed:
    return make(f"ℹ️ {title}", description, "info")


def cut(text, limit=1000) -> str:
    text = text or "*(vide)*"
    return text if len(text) <= limit else text[: limit - 1] + "…"
