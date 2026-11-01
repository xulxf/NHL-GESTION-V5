"""Extras communauté : mots interdits, anniversaires et meilleurs messages (starboard)."""
import calendar
import datetime
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import db, embeds
from utils.logs import send_log


def parse_words(text):
    """'mot1, mot2\nmot3' -> ['mot1', 'mot2', 'mot3'] (minuscules)."""
    return [w.strip().lower() for w in re.split(r"[,\n;]", text or "") if w.strip()]


def find_banned(content, words):
    """Premier mot interdit présent comme mot entier dans le message (sinon None)."""
    lowered = (content or "").lower()
    for word in words:
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", lowered):
            return word
    return None


def parse_birthday(text):
    """'25/12' ou '25/12/1999' -> (jour, mois), ou None si la date n'existe pas."""
    match = re.match(r"^\s*(\d{1,2})[/.-](\d{1,2})(?:[/.-]\d{2,4})?\s*$", text or "")
    if not match:
        return None
    day, month = int(match.group(1)), int(match.group(2))
    try:
        datetime.date(2000, month, day)  # 2000 est bissextile : le 29/02 est accepté
    except ValueError:
        return None
    return day, month


def birthday_keys(today):
    """Dates à fêter aujourd'hui (le 29/02 est fêté le 28/02 les années non bissextiles)."""
    keys = [(today.day, today.month)]
    if today.month == 2 and today.day == 28 and not calendar.isleap(today.year):
        keys.append((29, 2))
    return keys


class Extras(commands.Cog):
    """Mots interdits, anniversaires, starboard."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.check_birthdays.start()

    async def cog_unload(self):
        self.check_birthdays.cancel()

    # ---------- Mots interdits ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not isinstance(message.author, discord.Member):
            return
        raw = db.get_config(message.guild.id, "banned_words")
        if not raw or message.author.guild_permissions.manage_messages:
            return
        word = find_banned(message.content, parse_words(raw))
        if word is None:
            return
        try:
            await message.delete()
        except discord.HTTPException:
            return
        await message.channel.send(
            embed=embeds.error(f"{message.author.mention}, ce message contenait un mot interdit.", "Automod"), delete_after=8
        )
        log = embeds.make("🧼 Mot interdit supprimé", None, "warn")
        log.add_field(name="Auteur", value=message.author.mention)
        log.add_field(name="Salon", value=message.channel.mention)
        log.add_field(name="Mot", value=word)
        await send_log(message.guild, log)

    # ---------- Anniversaires ----------
    @app_commands.command(name="anniversaire", description="Enregistrer ton anniversaire (JJ/MM)")
    @app_commands.guild_only()
    @app_commands.describe(date="Ex : 25/12 (l'année n'est pas conservée)")
    async def anniversaire(self, interaction: discord.Interaction, date: str):
        parsed = parse_birthday(date)
        if parsed is None:
            return await interaction.response.send_message(embed=embeds.error("Date invalide. Exemple : `25/12`."), ephemeral=True)
        day, month = parsed
        db.run(
            "INSERT INTO birthdays (guild_id, user_id, day, month) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET day=excluded.day, month=excluded.month",
            (interaction.guild_id, interaction.user.id, day, month),
        )
        await interaction.response.send_message(embed=embeds.success(f"Anniversaire enregistré : **{day:02d}/{month:02d}** 🎂"), ephemeral=True)

    @app_commands.command(name="anniversaire-retirer", description="Supprimer ton anniversaire")
    @app_commands.guild_only()
    async def anniversaire_retirer(self, interaction: discord.Interaction):
        db.run("DELETE FROM birthdays WHERE guild_id=? AND user_id=?", (interaction.guild_id, interaction.user.id))
        await interaction.response.send_message(embed=embeds.success("Anniversaire supprimé."), ephemeral=True)

    @tasks.loop(minutes=30)
    async def check_birthdays(self):
        today = datetime.datetime.now(datetime.timezone.utc)
        stamp = today.strftime("%Y-%m-%d")
        for guild in self.bot.guilds:
            channel = guild.get_channel(db.get_int(guild.id, "birthday_channel") or 0)
            if channel is None or db.get_config(guild.id, "birthday_last") == stamp:
                continue
            db.set_config(guild.id, "birthday_last", stamp)  # d'abord : jamais deux messages le même jour
            members = []
            for day, month in birthday_keys(today):
                for row in db.rows("SELECT user_id FROM birthdays WHERE guild_id=? AND day=? AND month=?", (guild.id, day, month)):
                    member = guild.get_member(row["user_id"])
                    if member:
                        members.append(member)
            if members:
                try:
                    await channel.send(embed=embeds.make("🎂 Joyeux anniversaire !", ", ".join(m.mention for m in members), "success"))
                except discord.HTTPException:
                    logging.warning("Impossible d'envoyer les anniversaires")

    @check_birthdays.before_loop
    async def before_birthdays(self):
        await self.bot.wait_until_ready()

    # ---------- Meilleurs messages (⭐) ----------
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.guild_id is None or str(payload.emoji) != "⭐":
            return
        guild = self.bot.get_guild(payload.guild_id)
        star_channel = guild.get_channel(db.get_int(guild.id, "star_channel") or 0) if guild else None
        if star_channel is None or payload.channel_id == star_channel.id:
            return
        if db.one("SELECT 1 FROM starboard WHERE guild_id=? AND message_id=?", (guild.id, payload.message_id)):
            return
        channel = guild.get_channel(payload.channel_id)
        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.HTTPException, AttributeError):
            return
        if message.author.bot:
            return
        reaction = discord.utils.get(message.reactions, emoji="⭐")
        count = reaction.count if reaction else 0
        if count < int(db.get_config(guild.id, "star_threshold", "3") or 3):
            return
        try:
            db.run("INSERT INTO starboard (guild_id, message_id) VALUES (?, ?)", (guild.id, message.id))
        except Exception:  # deux étoiles au même instant : l'autre a déjà publié le message
            return
        embed = embeds.make("⭐ Meilleur message", (message.content or "*(pas de texte)*")[:2000], "warn")
        embed.add_field(name="Auteur", value=message.author.mention)
        embed.add_field(name="Salon", value=channel.mention)
        embed.add_field(name="Étoiles", value=f"⭐ {count}")
        embed.add_field(name="Message", value=f"[Aller au message]({message.jump_url})", inline=False)
        for attachment in message.attachments:
            if (attachment.content_type or "").startswith("image/"):
                embed.set_image(url=attachment.url)
                break
        await star_channel.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Extras(bot))
