"""Outils de fiabilité et d'administration : rapport d'erreurs, état du bot, annonces, achats Tebex."""
import logging
import os
import platform
import sys
import time
import traceback

import discord
from discord import app_commands
from discord.ext import commands

from cogs.tickets import create_system_ticket
from utils import db, embeds
from utils.logs import send_log


class Tools(commands.Cog):
    """Rapport d'erreurs dans Discord, /etat, annonces envoyées depuis le site et achats Tebex."""

    def __init__(self, bot):
        self.bot = bot
        self.started = time.time()
        self.recent = {}  # message d'erreur -> dernier envoi (anti-spam)
        self._old_tree_error = None
        self._old_on_error = None

    async def cog_load(self):
        # On « enveloppe » les gestionnaires d'erreurs existants : leur comportement ne change pas.
        self._old_tree_error = self.bot.tree.on_error

        async def tree_error(interaction, error):
            await self._old_tree_error(interaction, error)
            expected = (app_commands.MissingPermissions, app_commands.BotMissingPermissions, app_commands.CheckFailure)
            if isinstance(error, expected):
                return
            command = getattr(interaction.command, "qualified_name", "?")
            await self.report(interaction.guild_id, f"Commande /{command}", getattr(error, "original", error))

        self.bot.tree.on_error = tree_error

        self._old_on_error = self.bot.on_error

        async def on_error(event_method, *args, **kwargs):
            await self._old_on_error(event_method, *args, **kwargs)
            await self.report(None, f"Événement {event_method}", sys.exc_info()[1])

        self.bot.on_error = on_error

    async def cog_unload(self):
        if self._old_tree_error:
            self.bot.tree.on_error = self._old_tree_error
        if self._old_on_error:
            self.bot.on_error = self._old_on_error

    async def report(self, guild_id, title, error):
        """Envoie le détail d'une erreur dans le salon d'erreurs (si configuré)."""
        if error is None:
            return
        guilds = [self.bot.get_guild(guild_id)] if guild_id else list(self.bot.guilds)
        targets = []
        for guild in filter(None, guilds):
            channel = guild.get_channel(db.get_int(guild.id, "error_channel") or 0)
            if channel is not None:
                targets.append(channel)
        if not targets:  # aucun salon d'erreurs configuré : rien à envoyer
            return

        key = f"{title}:{error}"
        now = time.time()
        if now - self.recent.get(key, 0) < 60:  # la même erreur n'est signalée qu'une fois par minute
            return
        self.recent[key] = now

        text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        embed = embeds.make(f"🚨 {title}", f"```py\n{text[-1500:]}\n```", "error")
        for channel in targets:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                logging.warning("Impossible d'envoyer le rapport d'erreur")

    # ---------- Commandes ----------
    @app_commands.command(name="erreurs", description="Choisir le salon où le bot signale ses erreurs")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def erreurs(self, interaction: discord.Interaction, salon: discord.TextChannel):
        db.set_config(interaction.guild_id, "error_channel", salon.id)
        await interaction.response.send_message(embed=embeds.success(f"Les erreurs seront signalées dans {salon.mention}."), ephemeral=True)

    @app_commands.command(name="etat", description="État du bot : en ligne depuis, latence, base de données")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def etat(self, interaction: discord.Interaction):
        start = time.perf_counter()
        try:
            db.one("SELECT 1 AS ok")
            db_text = f"✅ {'PostgreSQL' if db.use_postgres() else 'SQLite locale'} · {(time.perf_counter() - start) * 1000:.0f} ms"
        except Exception:
            db_text = "❌ ne répond pas"
        gid = interaction.guild_id
        opened = db.one("SELECT COUNT(*) AS c FROM tickets WHERE guild_id=? AND closed_at IS NULL", (gid,))["c"]
        files = int(db.one("SELECT COALESCE(SUM(file_size), 0) AS s FROM resources")["s"] or 0)

        embed = embeds.make("🩺 État du bot", None, "info")
        embed.add_field(name="En ligne depuis", value=f"<t:{int(self.started)}:R>")
        embed.add_field(name="Latence Discord", value=f"{self.bot.latency * 1000:.0f} ms")
        embed.add_field(name="Base de données", value=db_text, inline=False)
        embed.add_field(name="Serveurs / membres", value=f"{len(self.bot.guilds)} / {sum(g.member_count or 0 for g in self.bot.guilds)}")
        embed.add_field(name="Tickets ouverts", value=str(opened))
        embed.add_field(name="Fichiers stockés", value=f"{files / 1048576:.0f} Mo")
        embed.add_field(name="Versions", value=f"Python {platform.python_version()} · discord.py {discord.__version__}", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- Appelé par le dashboard (dans la boucle du bot) ----------
    async def send_announcement(self, guild_id, channel_id, title, text, color):
        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild else None
        if channel is None:
            raise ValueError("Salon introuvable.")
        embed = embeds.make(title or None, text, color, section="welcome")
        message = await channel.send(embed=embed)
        return message.jump_url

    async def handle_purchase(self, summary, discord_id):
        """Achat Tebex terminé : rôle client + ticket de livraison (si l'acheteur est reconnu)."""
        dev = os.getenv("DEV_GUILD_ID", "").strip()
        guilds = [g for g in self.bot.guilds if not dev.isdigit() or g.id == int(dev)]
        handled = False
        for guild in guilds:
            member = guild.get_member(discord_id) if discord_id else None
            if member is None:
                continue
            handled = True
            role = guild.get_role(db.get_int(guild.id, "client_role") or 0)
            if role:
                try:
                    await member.add_roles(role, reason="Achat Tebex")
                except discord.HTTPException:
                    pass
            fields = [("🛍️ Produits", ", ".join(summary["products"])), ("💶 Montant", summary["amount"] or "—"), ("🔖 Transaction", summary["transaction"] or "—")]
            channel = await create_system_ticket(
                guild, member, "livraison", "📦 Livraison de commande", fields,
                intro="Merci pour ton achat ! L'équipe va te livrer ta commande.",
            )
            log = embeds.make("💳 Achat Tebex", f"{member.mention} a acheté **{', '.join(summary['products'])}**.", "success")
            log.add_field(name="Ticket", value=channel.mention)
            await send_log(guild, log)
        if not handled and guilds:
            log = embeds.make(
                "💳 Achat Tebex sans compte Discord reconnu",
                f"**{', '.join(summary['products'])}** ({summary['amount'] or 'montant inconnu'}) acheté par **{summary['buyer']}**.\n"
                "Aucun identifiant Discord valide dans la commande : à traiter à la main.",
                "warn",
            )
            await send_log(guilds[0], log)
        return handled


async def setup(bot):
    await bot.add_cog(Tools(bot))
