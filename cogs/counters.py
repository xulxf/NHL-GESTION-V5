"""Compteur de membres : dans le statut du bot (mis à jour chaque minute) et dans un salon vocal.

Discord limite le renommage d'un salon à 2 fois toutes les 10 minutes : le nom du salon est donc
actualisé toutes les 6 minutes. Le statut du bot, lui, peut changer beaucoup plus souvent.
"""
import logging
import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import db, embeds

STATUS_SECONDS = 60     # le statut alterne chaque minute entre ton texte et le nombre de membres
CHANNEL_MINUTES = 6     # mise à jour du nom du salon compteur


def counter_name(count: int) -> str:
    return f"👥 Membres : {count}"


class Counters(commands.Cog):
    """Nombre de membres affiché en direct."""

    def __init__(self, bot):
        self.bot = bot
        self.tick = 0
        self.last_names = {}  # id du salon -> dernier nom envoyé à Discord

    async def cog_load(self):
        self.update_status.start()
        self.update_channels.start()

    async def cog_unload(self):
        self.update_status.cancel()
        self.update_channels.cancel()

    def member_total(self) -> int:
        """Membres du serveur principal (DEV_GUILD_ID) ou de tous les serveurs du bot."""
        dev = os.getenv("DEV_GUILD_ID", "").strip()
        guild = self.bot.get_guild(int(dev)) if dev.isdigit() else None
        if guild is not None:
            return guild.member_count or 0
        return sum(g.member_count or 0 for g in self.bot.guilds)

    # ---------- Statut du bot ----------
    @tasks.loop(seconds=STATUS_SECONDS)
    async def update_status(self):
        if db.get_config(0, "status_counter", "1") != "1":
            return
        self.tick += 1
        texts = [os.getenv("STATUS_TEXT", "lux.fx"), f"👥 {self.member_total()} membres"]
        fivem = self.bot.get_cog("FiveM")
        players = fivem.status_text() if fivem else None
        if players:
            texts.append(players)
        text = texts[(self.tick - 1) % len(texts)]
        try:
            await self.bot.change_presence(
                status=self.bot.status,  # garde « Ne pas déranger » (ou le statut défini dans main.py)
                activity=discord.Activity(type=discord.ActivityType.watching, name=text),
            )
        except Exception:
            logging.exception("Mise à jour du statut impossible")

    @update_status.before_loop
    async def before_status(self):
        await self.bot.wait_until_ready()

    # ---------- Salon compteur ----------
    @tasks.loop(minutes=CHANNEL_MINUTES)
    async def update_channels(self):
        for guild in self.bot.guilds:
            channel_id = db.get_int(guild.id, "counter_channel")
            channel = guild.get_channel(channel_id) if channel_id else None
            if channel is None:
                continue
            name = counter_name(guild.member_count or 0)
            if channel.name == name or self.last_names.get(channel.id) == name:
                continue
            try:
                await channel.edit(name=name, reason="Compteur de membres")
                self.last_names[channel.id] = name
            except discord.HTTPException:
                logging.warning("Impossible de renommer le salon compteur de %s", guild.name)

    @update_channels.before_loop
    async def before_channels(self):
        await self.bot.wait_until_ready()

    # ---------- Commandes ----------
    @app_commands.command(name="membres", description="Nombre de membres du serveur")
    @app_commands.guild_only()
    async def membres(self, interaction: discord.Interaction):
        guild = interaction.guild
        bots = sum(1 for m in guild.members if m.bot)
        embed = embeds.make("👥 Membres", None, "info")
        embed.add_field(name="Total", value=f"**{guild.member_count}**")
        embed.add_field(name="Humains", value=f"**{guild.member_count - bots}**")
        embed.add_field(name="Bots", value=f"**{bots}**")
        await interaction.response.send_message(embed=embed)

    grp = app_commands.Group(
        name="compteur",
        description="Compteur de membres",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @grp.command(name="creer", description="Créer le salon vocal qui affiche le nombre de membres")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @app_commands.describe(categorie="Catégorie où placer le salon (facultatif)")
    async def creer(self, interaction: discord.Interaction, categorie: Optional[discord.CategoryChannel] = None):
        guild = interaction.guild
        existing_id = db.get_int(guild.id, "counter_channel")
        if existing_id and guild.get_channel(existing_id):
            return await interaction.response.send_message(
                embed=embeds.error("Le salon compteur existe déjà. Utilise `/compteur supprimer` pour le refaire."),
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True),
        }
        channel = await guild.create_voice_channel(
            name=counter_name(guild.member_count or 0),
            category=categorie,
            overwrites=overwrites,
            position=0,
            reason="Compteur de membres",
        )
        db.set_config(guild.id, "counter_channel", channel.id)
        self.last_names[channel.id] = channel.name
        await interaction.followup.send(
            embed=embeds.success(
                f"Salon créé : {channel.mention}\nSon nom est mis à jour toutes les {CHANNEL_MINUTES} minutes "
                "(Discord limite les changements de nom)."
            ),
            ephemeral=True,
        )

    @grp.command(name="supprimer", description="Supprimer le salon compteur")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def supprimer(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel_id = db.get_int(guild.id, "counter_channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is not None:
            try:
                await channel.delete(reason="Compteur de membres supprimé")
            except discord.HTTPException:
                return await interaction.response.send_message(
                    embed=embeds.error("Je n'ai pas pu supprimer le salon (permission « Gérer les salons » manquante ?)."),
                    ephemeral=True,
                )
        db.delete_config(guild.id, "counter_channel")
        await interaction.response.send_message(embed=embeds.success("Compteur supprimé."), ephemeral=True)

    @grp.command(name="statut", description="Afficher (ou non) le nombre de membres dans le statut du bot")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def statut(self, interaction: discord.Interaction, actif: bool):
        db.set_config(0, "status_counter", "1" if actif else "0")
        text = "Le statut alterne maintenant avec le nombre de membres." if actif else "Le statut n'affiche plus le nombre de membres."
        await interaction.response.send_message(embed=embeds.success(text), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Counters(bot))
