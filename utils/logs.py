import discord

from utils import db


async def send_log(guild: discord.Guild, embed: discord.Embed, file: discord.File = None, content: str = None):
    """Envoie un embed dans le salon de logs configuré (s'il existe)."""
    channel_id = db.get_int(guild.id, "log_channel")
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel is None:
        return
    try:
        await channel.send(content=content, embed=embed, file=file)
    except discord.HTTPException:
        pass
