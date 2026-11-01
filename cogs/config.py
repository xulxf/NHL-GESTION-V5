from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import db, embeds


class Config(commands.Cog):
    """Configuration du bot, directement depuis Discord."""

    def __init__(self, bot):
        self.bot = bot

    grp = app_commands.Group(
        name="setup",
        description="Configurer le bot",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    @grp.command(name="bienvenue", description="Définir le salon des messages de bienvenue")
    async def bienvenue(self, interaction: discord.Interaction, salon: discord.TextChannel):
        db.set_config(interaction.guild_id, "welcome_channel", salon.id)
        await interaction.response.send_message(
            embed=embeds.success(f"Salon de bienvenue : {salon.mention}"), ephemeral=True
        )

    @grp.command(name="logs", description="Définir le salon des logs")
    async def logs(self, interaction: discord.Interaction, salon: discord.TextChannel):
        db.set_config(interaction.guild_id, "log_channel", salon.id)
        await interaction.response.send_message(
            embed=embeds.success(f"Salon de logs : {salon.mention}"), ephemeral=True
        )

    @grp.command(name="role-auto", description="Rôle donné automatiquement aux nouveaux membres")
    async def role_auto(self, interaction: discord.Interaction, role: discord.Role):
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(
                embed=embeds.error("Ce rôle est au-dessus du mien, je ne pourrai pas le donner."),
                ephemeral=True,
            )
        db.set_config(interaction.guild_id, "auto_role", role.id)
        await interaction.response.send_message(
            embed=embeds.success(f"Rôle automatique : {role.mention}"), ephemeral=True
        )

    @grp.command(name="staff", description="Rôle de l'équipe (accès aux tickets)")
    async def staff(self, interaction: discord.Interaction, role: discord.Role):
        db.set_config(interaction.guild_id, "staff_role", role.id)
        await interaction.response.send_message(
            embed=embeds.success(f"Rôle staff : {role.mention}"), ephemeral=True
        )

    @grp.command(name="ticket-categorie", description="Catégorie Discord où créer les tickets")
    async def ticket_categorie(self, interaction: discord.Interaction, categorie: discord.CategoryChannel):
        db.set_config(interaction.guild_id, "ticket_category", categorie.id)
        await interaction.response.send_message(
            embed=embeds.success(f"Les tickets seront créés dans **{categorie.name}**."), ephemeral=True
        )

    @grp.command(name="automod", description="Activer ou désactiver l'automodération")
    @app_commands.describe(
        spam="Anti-spam (mise en sourdine automatique)",
        invites="Blocage des invitations Discord externes",
    )
    async def automod(
        self,
        interaction: discord.Interaction,
        spam: Optional[bool] = None,
        invites: Optional[bool] = None,
    ):
        if spam is not None:
            db.set_config(interaction.guild_id, "automod_spam", "1" if spam else "0")
        if invites is not None:
            db.set_config(interaction.guild_id, "automod_invites", "1" if invites else "0")
        await interaction.response.send_message(
            embed=embeds.success("Automodération mise à jour. Utilise `/setup voir` pour vérifier."),
            ephemeral=True,
        )

    @grp.command(name="avis", description="Salon où publier les avis clients")
    async def avis(self, interaction: discord.Interaction, salon: discord.TextChannel):
        db.set_config(interaction.guild_id, "review_channel", salon.id)
        await interaction.response.send_message(
            embed=embeds.success(f"Les avis clients seront publiés dans {salon.mention}."), ephemeral=True
        )

    @grp.command(name="vitrine", description="Salon de la vitrine des réalisations")
    async def vitrine(self, interaction: discord.Interaction, salon: discord.TextChannel):
        db.set_config(interaction.guild_id, "showcase_channel", salon.id)
        await interaction.response.send_message(
            embed=embeds.success(f"Vitrine : {salon.mention}"), ephemeral=True
        )

    @grp.command(name="suggestions", description="Salon des suggestions")
    async def suggestions(self, interaction: discord.Interaction, salon: discord.TextChannel):
        db.set_config(interaction.guild_id, "suggestion_channel", salon.id)
        await interaction.response.send_message(
            embed=embeds.success(f"Suggestions : {salon.mention}"), ephemeral=True
        )

    @grp.command(name="role-client", description="Rôle donné aux clients une fois leur commande livrée")
    async def role_client(self, interaction: discord.Interaction, role: discord.Role):
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(
                embed=embeds.error("Ce rôle est au-dessus du mien, je ne pourrai pas le donner."), ephemeral=True
            )
        db.set_config(interaction.guild_id, "client_role", role.id)
        await interaction.response.send_message(
            embed=embeds.success(f"Rôle client : {role.mention}"), ephemeral=True
        )

    @grp.command(name="role-reglement", description="Rôle donné quand un membre accepte le règlement")
    async def role_reglement(self, interaction: discord.Interaction, role: discord.Role):
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(
                embed=embeds.error("Ce rôle est au-dessus du mien, je ne pourrai pas le donner."), ephemeral=True
            )
        db.set_config(interaction.guild_id, "rules_role", role.id)
        await interaction.response.send_message(
            embed=embeds.success(f"Rôle du règlement : {role.mention}"), ephemeral=True
        )

    @grp.command(name="antiraid", description="Protection contre les arrivées massives de comptes")
    @app_commands.describe(mode="Que faire quand un raid est détecté")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Désactivé", value="off"),
        app_commands.Choice(name="Alerte uniquement", value="alerte"),
        app_commands.Choice(name="Alerte + sourdine des nouveaux", value="timeout"),
        app_commands.Choice(name="Alerte + expulsion des nouveaux", value="kick"),
    ])
    async def antiraid(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        db.set_config(interaction.guild_id, "antiraid", mode.value)
        await interaction.response.send_message(
            embed=embeds.success(f"Anti-raid : **{mode.name}**"), ephemeral=True
        )

    @grp.command(name="voir", description="Afficher la configuration actuelle")
    async def voir(self, interaction: discord.Interaction):
        guild = interaction.guild

        def channel(key):
            cid = db.get_int(guild.id, key)
            obj = guild.get_channel(cid) if cid else None
            return obj.mention if obj else "*non défini*"

        def role(key):
            rid = db.get_int(guild.id, key)
            obj = guild.get_role(rid) if rid else None
            return obj.mention if obj else "*non défini*"

        def toggle(key):
            return "✅ activé" if db.get_config(guild.id, key, "1") == "1" else "❌ désactivé"

        embed = embeds.make("⚙️ Configuration du bot", None, "info")
        embed.add_field(name="Bienvenue", value=channel("welcome_channel"))
        embed.add_field(name="Logs", value=channel("log_channel"))
        embed.add_field(name="Rôle auto", value=role("auto_role"))
        embed.add_field(name="Rôle staff", value=role("staff_role"))
        cat_id = db.get_int(guild.id, "ticket_category")
        cat = guild.get_channel(cat_id) if cat_id else None
        embed.add_field(name="Catégorie tickets", value=cat.name if cat else "*non définie*")
        embed.add_field(name="Avis", value=channel("review_channel"))
        embed.add_field(name="Vitrine", value=channel("showcase_channel"))
        embed.add_field(name="Suggestions", value=channel("suggestion_channel"))
        embed.add_field(name="Rôle client", value=role("client_role"))
        embed.add_field(name="Rôle règlement", value=role("rules_role"))
        embed.add_field(name="Anti-raid", value=db.get_config(guild.id, "antiraid", "alerte"))
        orders = "✅ ouvertes" if db.get_config(guild.id, "orders_open", "1") == "1" else "⛔ fermées"
        embed.add_field(name="Commandes", value=orders)
        embed.add_field(name="Anti-spam", value=toggle("automod_spam"))
        embed.add_field(name="Anti-invitations", value=toggle("automod_invites"))
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Config(bot))
