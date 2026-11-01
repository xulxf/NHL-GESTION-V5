import time

import discord
from discord.ext import commands

from utils import branding, db, embeds
from utils.logs import send_log


class Events(commands.Cog):
    """Bienvenue, rôle automatique et journal des événements."""

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        if not member.bot:
            db.run("INSERT INTO member_events (guild_id, kind, created_at) VALUES (?, 'join', ?)", (guild.id, int(time.time())))

        # Rôle automatique
        role_id = db.get_int(guild.id, "auto_role")
        role = guild.get_role(role_id) if role_id else None
        if role:
            try:
                await member.add_roles(role, reason="Rôle automatique")
            except discord.HTTPException:
                pass

        # Message de bienvenue
        channel_id = db.get_int(guild.id, "welcome_channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel:
            def fill(text):
                return (text.replace("{membre}", member.mention).replace("{nom}", member.display_name)
                        .replace("{serveur}", guild.name).replace("{numero}", str(guild.member_count)))

            title = fill(db.get_config(guild.id, "welcome_title") or "👋 Bienvenue {nom} !")
            text = fill(db.get_config(guild.id, "welcome_text") or (
                "{membre}, ravi de t'accueillir sur **{serveur}** !\nPense à lire le règlement et à te présenter."))
            embed = embeds.make(
                title[:256],
                text[:4000],
                "success",
                footer=f"Tu es le membre n°{guild.member_count}",
                section="welcome",
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            banner = branding.image("welcome")
            if banner:
                embed.set_image(url=banner)
            await channel.send(content=member.mention, embed=embed)

        # Log
        log = embeds.make("📥 Nouveau membre", f"{member.mention} (`{member.id}`)", "success")
        log.add_field(
            name="Compte créé", value=discord.utils.format_dt(member.created_at, "R")
        )
        log.set_thumbnail(url=member.display_avatar.url)
        await send_log(guild, log)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if not member.bot:
            db.run("INSERT INTO member_events (guild_id, kind, created_at) VALUES (?, 'leave', ?)", (member.guild.id, int(time.time())))
        log = embeds.make("📤 Départ", f"**{member}** (`{member.id}`) a quitté le serveur.", "error")
        log.set_thumbnail(url=member.display_avatar.url)
        await send_log(member.guild, log)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        log = embeds.make("🗑️ Message supprimé", None, "warn")
        log.add_field(name="Auteur", value=message.author.mention)
        log.add_field(name="Salon", value=message.channel.mention)
        log.add_field(name="Contenu", value=embeds.cut(message.content), inline=False)
        await send_log(message.guild, log)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not before.guild or before.author.bot or before.content == after.content:
            return
        log = embeds.make("✏️ Message modifié", f"[Aller au message]({after.jump_url})", "info")
        log.add_field(name="Auteur", value=before.author.mention)
        log.add_field(name="Salon", value=before.channel.mention)
        log.add_field(name="Avant", value=embeds.cut(before.content), inline=False)
        log.add_field(name="Après", value=embeds.cut(after.content), inline=False)
        await send_log(before.guild, log)


async def setup(bot):
    await bot.add_cog(Events(bot))
