from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import embeds


class Utility(commands.Cog):
    """Commandes d'information."""

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Latence du bot")
    async def ping(self, interaction: discord.Interaction):
        ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(embed=embeds.make("🏓 Pong !", f"Latence : **{ms} ms**", "info"))

    @app_commands.command(name="serverinfo", description="Informations sur le serveur")
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction):
        g = interaction.guild
        humans = sum(1 for m in g.members if not m.bot)
        embed = embeds.make(f"📊 {g.name}", g.description, "info", footer=f"ID : {g.id}")
        embed.add_field(name="Propriétaire", value=f"<@{g.owner_id}>")
        embed.add_field(name="Membres", value=f"{g.member_count} ({humans} humains)")
        embed.add_field(name="Salons", value=f"{len(g.text_channels)} textuels • {len(g.voice_channels)} vocaux")
        embed.add_field(name="Rôles", value=str(len(g.roles) - 1))
        embed.add_field(name="Boosts", value=f"{g.premium_subscription_count} (niveau {g.premium_tier})")
        embed.add_field(name="Créé le", value=discord.utils.format_dt(g.created_at, "D"))
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="userinfo", description="Informations sur un membre")
    @app_commands.guild_only()
    async def userinfo(self, interaction: discord.Interaction, membre: Optional[discord.Member] = None):
        membre = membre or interaction.user
        roles = [r.mention for r in reversed(membre.roles[1:])][:10]
        embed = embeds.make(f"👤 {membre.display_name}", None, "info")
        embed.add_field(name="Identifiant", value=f"`{membre.id}`")
        embed.add_field(name="Compte créé", value=discord.utils.format_dt(membre.created_at, "D"))
        embed.add_field(name="A rejoint", value=discord.utils.format_dt(membre.joined_at, "D") if membre.joined_at else "?")
        embed.add_field(name="Rôles", value=" ".join(roles) or "Aucun", inline=False)
        embed.set_thumbnail(url=membre.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="avatar", description="Afficher l'avatar d'un membre")
    async def avatar(self, interaction: discord.Interaction, membre: Optional[discord.Member] = None):
        membre = membre or interaction.user
        embed = embeds.make(f"🖼️ Avatar de {membre.display_name}", None, "info")
        embed.set_image(url=membre.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="help", description="Liste des commandes")
    async def help(self, interaction: discord.Interaction):
        embed = embeds.make("📖 Aide", "Voici ce que je sais faire :", "info")
        embed.add_field(
            name="🔨 Modération",
            value="`/kick` `/ban` `/unban` `/timeout` `/untimeout`\n`/warn` `/warnings` `/delwarn` `/clearwarns`\n`/clear` `/slowmode` `/lock` `/unlock`",
            inline=False,
        )
        embed.add_field(name="🎫 Tickets", value="`/ticket-panel` `/ticket-add` `/ticket-remove`", inline=False)
        embed.add_field(name="ℹ️ Infos", value="`/serverinfo` `/userinfo` `/avatar` `/ping`", inline=False)
        embed.add_field(
            name="⚙️ Administration",
            value="`/setup bienvenue` `/setup logs` `/setup role-auto`\n`/setup staff` `/setup ticket-categorie`\n`/setup avis` `/setup vitrine` `/setup suggestions`\n`/setup role-client` `/setup role-reglement`\n`/setup antiraid` `/setup automod` `/setup voir`",
            inline=False,
        )
        embed.add_field(
            name="📦 Commandes (équipe)",
            value="`/statut` `/commandes ouvrir` `/commandes fermer`\n`/realisation` `/stats` `/faq-admin`",
            inline=False,
        )
        embed.add_field(
            name="🎉 Communauté",
            value="`/faq` `/rank` `/classement` `/suggestion`\n(équipe) `/giveaway` `/sondage` `/reglement` `/niveaux` `/message`",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Utility(bot))
