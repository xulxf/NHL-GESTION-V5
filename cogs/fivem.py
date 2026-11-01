"""FiveM : joueurs connectés en direct et candidatures de whitelist."""
import json
import logging
import time
import urllib.parse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.tickets import is_staff
from utils import db, embeds


def valid_server_url(url):
    parsed = urllib.parse.urlparse(url or "")
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def parse_players(data):
    """players.json -> liste de pseudos."""
    if not isinstance(data, list):
        return []
    return [str(p.get("name", "?"))[:32] for p in data if isinstance(p, dict)]


def parse_max(info):
    try:
        return int(((info or {}).get("vars") or {}).get("sv_maxClients"))
    except (TypeError, ValueError):
        return None


# ---------- Whitelist ----------
def staff_or_roles(member):
    return is_staff(member) or member.guild_permissions.manage_roles


async def decide(interaction: discord.Interaction, accepted: bool):
    if not staff_or_roles(interaction.user):
        return await interaction.response.send_message(embed=embeds.error("Réservé à l'équipe."), ephemeral=True)
    app = db.one("SELECT * FROM applications WHERE message_id=? AND status='attente'", (interaction.message.id,))
    if app is None:
        return await interaction.response.send_message(embed=embeds.error("Cette candidature a déjà été traitée."), ephemeral=True)

    status = "acceptee" if accepted else "refusee"
    db.run("UPDATE applications SET status=? WHERE id=?", (status, app["id"]))
    guild = interaction.guild
    member = guild.get_member(app["user_id"])

    if accepted and member:
        role = guild.get_role(db.get_int(guild.id, "whitelist_role") or 0)
        if role:
            try:
                await member.add_roles(role, reason="Whitelist acceptée")
            except discord.HTTPException:
                pass
    if member:
        text = "Ta candidature a été **acceptée** ! Bienvenue 🎉" if accepted else "Ta candidature a été **refusée** pour le moment."
        try:
            await member.send(embed=embeds.make("📝 Candidature whitelist", f"Serveur : **{guild.name}**\n{text}", "success" if accepted else "error"))
        except (discord.Forbidden, discord.HTTPException):
            pass

    embed = interaction.message.embeds[0]
    embed.add_field(name="Décision", value=f"{'✅ Acceptée' if accepted else '❌ Refusée'} par {interaction.user.mention}", inline=False)
    await interaction.response.edit_message(embed=embed, view=None)


class ReviewView(discord.ui.View):
    """Boutons Accepter / Refuser sous chaque candidature (persistants)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Accepter", emoji="✅", style=discord.ButtonStyle.success, custom_id="wl:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await decide(interaction, True)

    @discord.ui.button(label="Refuser", emoji="❌", style=discord.ButtonStyle.danger, custom_id="wl:refuse")
    async def refuse(self, interaction: discord.Interaction, button: discord.ui.Button):
        await decide(interaction, False)


class WhitelistModal(discord.ui.Modal, title="Candidature whitelist"):
    pseudo = discord.ui.TextInput(label="Pseudo / nom RP", max_length=60)
    age = discord.ui.TextInput(label="Âge", max_length=3)
    experience = discord.ui.TextInput(label="Ton expérience en RP", style=discord.TextStyle.paragraph, max_length=800)
    motivation = discord.ui.TextInput(label="Pourquoi rejoindre le serveur ?", style=discord.TextStyle.paragraph, max_length=800)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel = guild.get_channel(db.get_int(guild.id, "whitelist_channel") or 0)
        if channel is None:
            return await interaction.response.send_message(embed=embeds.error("Les candidatures ne sont pas configurées."), ephemeral=True)
        if db.one("SELECT 1 FROM applications WHERE guild_id=? AND user_id=? AND status='attente'", (guild.id, interaction.user.id)):
            return await interaction.response.send_message(embed=embeds.error("Tu as déjà une candidature en attente."), ephemeral=True)

        answers = {
            "Pseudo / nom RP": self.pseudo.value, "Âge": self.age.value,
            "Expérience RP": self.experience.value, "Motivation": self.motivation.value,
        }
        app_id, _ = db.run(
            "INSERT INTO applications (guild_id, user_id, answers, created_at) VALUES (?, ?, ?, ?)",
            (guild.id, interaction.user.id, json.dumps(answers, ensure_ascii=False), int(time.time())),
        )
        embed = embeds.make(f"📝 Candidature #{app_id}", f"Candidat : {interaction.user.mention}", "info")
        for question, answer in answers.items():
            embed.add_field(name=question, value=answer[:1000] or "—", inline=question in ("Pseudo / nom RP", "Âge"))
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        message = await channel.send(embed=embed, view=ReviewView())
        db.run("UPDATE applications SET message_id=? WHERE id=?", (message.id, app_id))
        await interaction.response.send_message(embed=embeds.success("Candidature envoyée ! Tu recevras la réponse en message privé."), ephemeral=True)


class ApplyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Postuler à la whitelist", emoji="📝", style=discord.ButtonStyle.primary, custom_id="wl:apply")
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = interaction.guild.get_role(db.get_int(interaction.guild_id, "whitelist_role") or 0)
        if role and role in interaction.user.roles:
            return await interaction.response.send_message(embed=embeds.info("Tu es déjà whitelisté !"), ephemeral=True)
        await interaction.response.send_modal(WhitelistModal())


class FiveM(commands.Cog):
    """Joueurs en direct et candidatures de whitelist."""

    def __init__(self, bot):
        self.bot = bot
        self.stats = {}  # guild_id -> {"players": [...], "max": int|None, "online": bool}

    async def cog_load(self):
        self.bot.add_view(ApplyView())
        self.bot.add_view(ReviewView())
        self.poll.start()

    async def cog_unload(self):
        self.poll.cancel()

    async def fetch(self, base):
        base = base.rstrip("/")
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{base}/players.json") as response:
                players = parse_players(await response.json(content_type=None))
            max_clients = None
            try:
                async with session.get(f"{base}/info.json") as response:
                    max_clients = parse_max(await response.json(content_type=None))
            except Exception:
                pass
        return players, max_clients

    @tasks.loop(seconds=60)
    async def poll(self):
        for guild in self.bot.guilds:
            url = db.get_config(guild.id, "fivem_url")
            if not url or not valid_server_url(url):
                self.stats.pop(guild.id, None)
                continue
            try:
                players, max_clients = await self.fetch(url)
                self.stats[guild.id] = {"players": players, "max": max_clients, "online": True}
            except Exception:
                self.stats[guild.id] = {"players": [], "max": None, "online": False}
                logging.info("Serveur FiveM de %s injoignable", guild.name)

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    def status_text(self):
        """Texte « 🎮 12/64 joueurs » pour le statut du bot (None si pas de serveur configuré ou hors ligne)."""
        for stats in self.stats.values():
            if stats["online"]:
                count = len(stats["players"])
                return f"🎮 {count}/{stats['max']} joueurs" if stats["max"] else f"🎮 {count} joueurs"
        return None

    @app_commands.command(name="fivem", description="Joueurs connectés au serveur FiveM")
    @app_commands.guild_only()
    async def fivem(self, interaction: discord.Interaction):
        stats = self.stats.get(interaction.guild_id)
        if stats is None:
            return await interaction.response.send_message(embed=embeds.error("Aucun serveur FiveM configuré (dashboard → FiveM)."), ephemeral=True)
        if not stats["online"]:
            return await interaction.response.send_message(embed=embeds.make("🎮 Serveur FiveM", "🔴 Hors ligne ou injoignable.", "error"))
        count = len(stats["players"])
        total = f"/{stats['max']}" if stats["max"] else ""
        names = ", ".join(stats["players"][:25]) or "Personne pour l'instant"
        embed = embeds.make("🎮 Serveur FiveM", f"🟢 En ligne : **{count}{total}** joueur(s)", "success")
        embed.add_field(name="Connectés", value=names[:1000], inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="whitelist", description="Poster le panneau de candidature whitelist")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def whitelist(self, interaction: discord.Interaction):
        if not db.get_int(interaction.guild_id, "whitelist_channel"):
            return await interaction.response.send_message(
                embed=embeds.error("Choisis d'abord le salon des candidatures dans le dashboard (page FiveM)."), ephemeral=True
            )
        embed = embeds.make("📝 Whitelist", "Pour rejoindre le serveur, clique sur le bouton et réponds aux questions. L'équipe te répondra en message privé.", "info", section="ticket")
        await interaction.channel.send(embed=embed, view=ApplyView())
        await interaction.response.send_message(embed=embeds.success("Panneau envoyé."), ephemeral=True)


async def setup(bot):
    await bot.add_cog(FiveM(bot))
