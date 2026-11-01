"""Giveaways — fichier autonome (aucune autre dépendance que discord.py 2.x).

Principe : les participants sont les réactions 🎉 et les infos du giveaway sont lues dans l'embed du
message. Après un redémarrage du bot, les giveaways en cours sont retrouvés automatiquement :
aucune base de données n'est nécessaire.

Chargement : bot.load_extension("cogs.giveaways")
Le bot a besoin des permissions : Envoyer des messages, Intégrer des liens, Ajouter des réactions,
Voir l'historique. L'intent « Server Members » est recommandé.
"""
import asyncio
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

EMOJI = "🎉"
COLOR_ACTIVE = 0x5865F2
COLOR_ENDED = 0x57F287
COLOR_CANCELLED = 0xED4245
MAX_DURATION = 30 * 86400

ENDS_RE = re.compile(r"<t:(\d+):R>")
WINNERS_RE = re.compile(r"Gagnants(?: prévus)? : \*\*(\d+)\*\*")
HOST_RE = re.compile(r"Organisé par <@(\d+)>")
ROLE_RE = re.compile(r"<@&(\d+)>")
OLD_MARKER_RE = re.compile(r"\s*•?\s*gw(?:end)?:[\d:]+")  # ancien format (chiffres dans le pied de page)
_DURATION_RE = re.compile(r"(\d+)([smhdj])")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "j": 86400}


# ---------- Fonctions utilitaires ----------
def parse_duration(text: str):
    """'30m', '2h', '1j', '1h30m' -> secondes (None si invalide)."""
    cleaned = (text or "").replace(" ", "").lower()
    if not cleaned:
        return None
    pos, total = 0, 0
    for match in _DURATION_RE.finditer(cleaned):
        if match.start() != pos:
            return None
        total += int(match.group(1)) * _UNITS[match.group(2)]
        pos = match.end()
    return total if pos == len(cleaned) and total > 0 else None


def fmt_remaining(seconds) -> str:
    seconds = max(int(seconds), 0)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h {seconds % 3600 // 60:02d}"
    return f"{seconds // 86400} j {seconds % 86400 // 3600} h"


def extract_id(text: str) -> Optional[int]:
    """Accepte un identifiant de message ou un lien vers un message."""
    numbers = re.findall(r"\d{15,20}", text or "")
    return int(numbers[-1]) if numbers else None


def set_footer(embed: discord.Embed, text: str):
    brand = os.getenv("BRAND_NAME", "").strip()
    icon = os.getenv("LOGO_URL", "").strip() or None
    embed.set_footer(text=f"{text} • {brand}" if brand else text, icon_url=icon)


@dataclass
class Giveaway:
    message_id: int
    channel_id: int
    guild_id: int
    prize: str
    ends_at: int
    winners: int
    host_id: int
    role_id: int = 0

    @property
    def url(self) -> str:
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.message_id}"


def active_embed(g: Giveaway) -> discord.Embed:
    lines = [
        f"Réagis avec {EMOJI} pour participer !",
        "",
        f"⏰ Fin : <t:{g.ends_at}:R> (<t:{g.ends_at}:f>)",
        f"🏆 Gagnants : **{g.winners}**",
        f"🎙️ Organisé par <@{g.host_id}>",
    ]
    if g.role_id:
        lines.append(f"🔒 Réservé aux membres ayant le rôle <@&{g.role_id}>")
    embed = discord.Embed(title=f"🎁 {g.prize}", description="\n".join(lines), color=COLOR_ACTIVE)
    set_footer(embed, f"{g.winners} gagnant(s)")
    return embed


def ended_embed(g: Giveaway, mentions: str) -> discord.Embed:
    result = f"🏆 Gagnant(s) : {mentions}" if mentions else "😢 Aucun participant valide."
    lines = [result, "", f"🎙️ Organisé par <@{g.host_id}>", f"🏆 Gagnants prévus : **{g.winners}**"]
    if g.role_id:
        lines.append(f"🔒 Réservé aux membres ayant le rôle <@&{g.role_id}>")
    embed = discord.Embed(title=f"🎁 {g.prize} — Terminé", description="\n".join(lines), color=COLOR_ENDED)
    set_footer(embed, "Terminé")
    return embed


def cancelled_embed(g: Giveaway) -> discord.Embed:
    embed = discord.Embed(
        title=f"🎁 {g.prize} — Annulé",
        description="Ce giveaway a été annulé par l'équipe.",
        color=COLOR_CANCELLED,
    )
    set_footer(embed, "Annulé")
    return embed


class Giveaways(commands.Cog):
    """Giveaways avec durée, plusieurs gagnants et rôle requis."""

    def __init__(self, bot):
        self.bot = bot
        self.active = {}   # message_id -> Giveaway (en cours)
        self.ended = {}    # message_id -> Giveaway (terminés, pour /giveaway reroll)
        self._recovered = False
        self._recover_task = None

    async def cog_load(self):
        self.check_giveaways.start()
        if self.bot.is_ready():  # cog chargé alors que le bot est déjà connecté
            self._recovered = True
            self._recover_task = asyncio.create_task(self.recover())

    async def cog_unload(self):
        self.check_giveaways.cancel()

    # ---------- Récupération après un redémarrage ----------
    @commands.Cog.listener()
    async def on_ready(self):
        if not self._recovered:
            self._recovered = True
            await self.recover()

    def register_from_message(self, message: discord.Message):
        """Reconstruit un giveaway à partir de son embed (description), sans chiffres cachés."""
        if message.guild is None or message.author.id != self.bot.user.id or not message.embeds:
            return
        embed = message.embeds[0]
        title = embed.title or ""
        description = embed.description or ""
        host = HOST_RE.search(description)
        if not title.startswith("🎁 ") or not host or title.endswith(" — Annulé"):
            return

        winners = WINNERS_RE.search(description)
        role = ROLE_RE.search(description)
        role_id = int(role.group(1)) if role else 0
        prize = title.removeprefix("🎁 ").removesuffix(" — Terminé")
        count = int(winners.group(1)) if winners else 1

        if title.endswith(" — Terminé"):
            self.ended.setdefault(
                message.id,
                Giveaway(message.id, message.channel.id, message.guild.id, prize, 0, count, int(host.group(1)), role_id),
            )
            return
        ends = ENDS_RE.search(description)
        if ends and winners:
            self.active.setdefault(
                message.id,
                Giveaway(
                    message.id, message.channel.id, message.guild.id, prize,
                    int(ends.group(1)), count, int(host.group(1)), role_id,
                ),
            )

    async def clean_old_footer(self, message: discord.Message):
        """Retire les anciens chiffres du pied de page (ancien format) des messages du bot."""
        if message.author.id != self.bot.user.id or not message.embeds:
            return
        embed = message.embeds[0]
        text = embed.footer.text or ""
        if not OLD_MARKER_RE.search(text):
            return
        cleaned = OLD_MARKER_RE.sub("", text).strip(" •") or "Giveaway"
        embed.set_footer(text=cleaned, icon_url=embed.footer.icon_url)
        try:
            await message.edit(embed=embed)
        except discord.HTTPException:
            pass

    async def recover(self):
        """Retrouve les giveaways récents en parcourant les derniers messages du bot."""
        for guild in self.bot.guilds:
            for channel in guild.text_channels:
                perms = channel.permissions_for(guild.me)
                if not (perms.view_channel and perms.read_message_history):
                    continue
                try:
                    async for message in channel.history(limit=30):
                        self.register_from_message(message)
                        await self.clean_old_footer(message)
                except discord.HTTPException:
                    continue

    # ---------- Fin des giveaways ----------
    @tasks.loop(seconds=15)
    async def check_giveaways(self):
        now = int(time.time())
        for giveaway in [g for g in self.active.values() if g.ends_at <= now]:
            try:
                await self.finish(giveaway)
            except Exception:
                logging.exception("Erreur pendant la fin d'un giveaway")

    @check_giveaways.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    async def fetch_message(self, g: Giveaway):
        channel = self.bot.get_channel(g.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(g.channel_id)
            except discord.HTTPException:
                return None
        try:
            return await channel.fetch_message(g.message_id)
        except discord.HTTPException:
            return None

    @staticmethod
    async def eligible_members(message: discord.Message, g: Giveaway):
        reaction = discord.utils.get(message.reactions, emoji=EMOJI)
        if reaction is None:
            return []
        guild, members = message.guild, []
        async for user in reaction.users():
            if user.bot:
                continue
            member = guild.get_member(user.id)
            if member is None:
                try:
                    member = await guild.fetch_member(user.id)
                except discord.HTTPException:
                    continue
            if g.role_id and all(role.id != g.role_id for role in member.roles):
                continue
            members.append(member)
        return members

    async def finish(self, g: Giveaway, cancelled: bool = False):
        self.active.pop(g.message_id, None)
        message = await self.fetch_message(g)
        if message is None:
            return None

        if cancelled:
            await message.edit(embed=cancelled_embed(g))
            return []

        members = await self.eligible_members(message, g)
        winners = random.sample(members, min(g.winners, len(members)))
        mentions = ", ".join(w.mention for w in winners)
        await message.edit(embed=ended_embed(g, mentions))
        self.ended[g.message_id] = g
        if winners:
            await message.reply(
                content=f"🎉 Félicitations {mentions} ! Tu remportes **{g.prize}** !", mention_author=False
            )
        else:
            await message.reply("😢 Personne n'a participé à ce giveaway.", mention_author=False)
        return winners

    # ---------- Auto-complétion ----------
    async def autocomplete_active(self, interaction: discord.Interaction, current: str):
        now = int(time.time())
        choices = []
        for g in self.active.values():
            if g.guild_id == interaction.guild_id and current.lower() in g.prize.lower():
                name = f"{g.prize[:60]} (fin dans {fmt_remaining(g.ends_at - now)})"
                choices.append(app_commands.Choice(name=name, value=str(g.message_id)))
        return choices[:25]

    async def autocomplete_ended(self, interaction: discord.Interaction, current: str):
        choices = []
        for g in self.ended.values():
            if g.guild_id == interaction.guild_id and current.lower() in g.prize.lower():
                choices.append(app_commands.Choice(name=f"{g.prize[:80]} (terminé)", value=str(g.message_id)))
        return choices[-25:]

    # ---------- Commandes ----------
    grp = app_commands.Group(
        name="giveaway",
        description="Gérer les giveaways",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @grp.command(name="lancer", description="Lancer un giveaway")
    @app_commands.describe(
        prix="Ce qui est à gagner",
        duree="Durée : 30m, 2h, 1j, 1h30m…",
        gagnants="Nombre de gagnants",
        salon="Salon du giveaway (par défaut : ici)",
        role_requis="Rôle obligatoire pour participer (facultatif)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def lancer(
        self,
        interaction: discord.Interaction,
        prix: app_commands.Range[str, 1, 200],
        duree: str,
        gagnants: app_commands.Range[int, 1, 20] = 1,
        salon: Optional[discord.TextChannel] = None,
        role_requis: Optional[discord.Role] = None,
    ):
        seconds = parse_duration(duree)
        if seconds is None or seconds < 10 or seconds > MAX_DURATION:
            return await interaction.response.send_message(
                "❌ Durée invalide. Exemples : `30m`, `2h`, `1j`, `1h30m` (de 10 secondes à 30 jours).",
                ephemeral=True,
            )
        channel = salon or interaction.channel
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.embed_links and perms.add_reactions and perms.read_message_history):
            return await interaction.response.send_message(
                f"❌ Il me manque des permissions dans {channel.mention} "
                "(Envoyer des messages, Intégrer des liens, Ajouter des réactions, Voir l'historique).",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        giveaway = Giveaway(
            message_id=0,
            channel_id=channel.id,
            guild_id=interaction.guild_id,
            prize=prix,
            ends_at=int(time.time()) + seconds,
            winners=gagnants,
            host_id=interaction.user.id,
            role_id=role_requis.id if role_requis else 0,
        )
        message = await channel.send(embed=active_embed(giveaway))
        await message.add_reaction(EMOJI)
        giveaway.message_id = message.id
        self.active[message.id] = giveaway
        await interaction.followup.send(f"✅ Giveaway lancé dans {channel.mention} : {message.jump_url}", ephemeral=True)

    def find_active(self, interaction: discord.Interaction, text: str):
        message_id = extract_id(text)
        giveaway = self.active.get(message_id) if message_id else None
        if giveaway and giveaway.guild_id == interaction.guild_id:
            return giveaway
        return None

    @grp.command(name="terminer", description="Terminer un giveaway maintenant et tirer les gagnants")
    @app_commands.describe(giveaway="Le giveaway à terminer")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def terminer(self, interaction: discord.Interaction, giveaway: str):
        found = self.find_active(interaction, giveaway)
        if found is None:
            return await interaction.response.send_message("❌ Giveaway en cours introuvable.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self.finish(found)
        await interaction.followup.send("✅ Giveaway terminé.", ephemeral=True)

    @terminer.autocomplete("giveaway")
    async def terminer_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.autocomplete_active(interaction, current)

    @grp.command(name="annuler", description="Annuler un giveaway sans tirer de gagnant")
    @app_commands.describe(giveaway="Le giveaway à annuler")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def annuler(self, interaction: discord.Interaction, giveaway: str):
        found = self.find_active(interaction, giveaway)
        if found is None:
            return await interaction.response.send_message("❌ Giveaway en cours introuvable.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self.finish(found, cancelled=True)
        await interaction.followup.send("✅ Giveaway annulé.", ephemeral=True)

    @annuler.autocomplete("giveaway")
    async def annuler_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.autocomplete_active(interaction, current)

    @grp.command(name="reroll", description="Tirer de nouveaux gagnants pour un giveaway terminé")
    @app_commands.describe(
        giveaway="Le giveaway terminé (ou l'identifiant / le lien du message, dans ce salon)",
        gagnants="Nombre de nouveaux gagnants (par défaut : comme à l'origine)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reroll(
        self,
        interaction: discord.Interaction,
        giveaway: str,
        gagnants: Optional[app_commands.Range[int, 1, 20]] = None,
    ):
        message_id = extract_id(giveaway)
        found = self.ended.get(message_id) if message_id else None
        await interaction.response.defer()

        if found is None and message_id:  # pas en mémoire : on cherche dans le salon actuel
            try:
                self.register_from_message(await interaction.channel.fetch_message(message_id))
            except discord.HTTPException:
                pass
            found = self.ended.get(message_id)
        if found is None or found.guild_id != interaction.guild_id:
            return await interaction.followup.send(
                "❌ Giveaway terminé introuvable. Lance la commande dans le salon du giveaway avec l'identifiant du message.",
                ephemeral=True,
            )

        message = await self.fetch_message(found)
        if message is None:
            return await interaction.followup.send("❌ Je ne retrouve plus le message du giveaway.", ephemeral=True)
        members = await self.eligible_members(message, found)
        if not members:
            return await interaction.followup.send("❌ Aucun participant valide pour un nouveau tirage.", ephemeral=True)
        winners = random.sample(members, min(gagnants or found.winners, len(members)))
        mentions = ", ".join(w.mention for w in winners)
        await interaction.followup.send(
            f"🔁 Nouveau tirage ! Félicitations {mentions}, tu remportes **{found.prize}** !\n{found.url}"
        )

    @reroll.autocomplete("giveaway")
    async def reroll_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.autocomplete_ended(interaction, current)

    @grp.command(name="liste", description="Lister les giveaways en cours")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def liste(self, interaction: discord.Interaction):
        now = int(time.time())
        items = sorted((g for g in self.active.values() if g.guild_id == interaction.guild_id), key=lambda g: g.ends_at)
        if not items:
            return await interaction.response.send_message("Aucun giveaway en cours.", ephemeral=True)
        lines = [f"🎁 **{g.prize}** — fin <t:{g.ends_at}:R> — <#{g.channel_id}> — [lien]({g.url})" for g in items]
        embed = discord.Embed(title="🎉 Giveaways en cours", description="\n".join(lines)[:4000], color=COLOR_ACTIVE)
        embed.set_footer(text=f"{len(items)} giveaway(s) • prochain dans {fmt_remaining(items[0].ends_at - now)}")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Giveaways(bot))
