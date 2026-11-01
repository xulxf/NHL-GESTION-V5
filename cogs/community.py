import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import db, embeds

NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]

SUGGESTION_STATUS = {
    "attente": ("⏳ En attente", "info"),
    "accepte": ("✅ Acceptée", "success"),
    "refuse": ("❌ Refusée", "error"),
    "etude": ("🔎 À l'étude", "warn"),
}
SUGGESTION_CHOICES = [
    app_commands.Choice(name=label, value=key)
    for key, (label, _) in SUGGESTION_STATUS.items() if key != "attente"
]


# ---------- Règlement avec bouton ----------
class RulesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="J'accepte le règlement", emoji="✅", style=discord.ButtonStyle.success, custom_id="rules:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        role_id = db.get_int(interaction.guild_id, "rules_role")
        role = interaction.guild.get_role(role_id) if role_id else None
        if role is None:
            return await interaction.response.send_message(
                embed=embeds.error("Aucun rôle n'est configuré (`/setup role-reglement`)."), ephemeral=True
            )
        if role in interaction.user.roles:
            return await interaction.response.send_message(
                embed=embeds.info("Tu as déjà accepté le règlement. Bienvenue !"), ephemeral=True
            )
        try:
            await interaction.user.add_roles(role, reason="Règlement accepté")
        except discord.Forbidden:
            return await interaction.response.send_message(
                embed=embeds.error("Je ne peux pas te donner ce rôle (mon rôle est trop bas)."), ephemeral=True
            )
        await interaction.response.send_message(
            embed=embeds.success(f"Merci ! Tu as reçu le rôle {role.mention}.", "Règlement accepté"), ephemeral=True
        )


class RulesModal(discord.ui.Modal, title="Règlement du serveur"):
    titre = discord.ui.TextInput(label="Titre", default="📜 Règlement", max_length=256)
    texte = discord.ui.TextInput(label="Règlement", style=discord.TextStyle.paragraph, max_length=4000)

    def __init__(self, channel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        embed = embeds.make(self.titre.value, self.texte.value, "info", section="welcome")
        embed.timestamp = None
        try:
            await self.channel.send(embed=embed, view=RulesView())
        except discord.HTTPException:
            return await interaction.response.send_message(
                embed=embeds.error("Impossible d'envoyer le règlement dans ce salon."), ephemeral=True
            )
        await interaction.response.send_message(embed=embeds.success("Règlement publié."), ephemeral=True)


# ---------- Suggestions ----------
def suggestion_embed(row, guild: discord.Guild) -> discord.Embed:
    label, kind = SUGGESTION_STATUS[row["status"]]
    embed = embeds.make(f"💡 Suggestion #{row['id']}", row["content"], kind)
    author = guild.get_member(row["user_id"])
    embed.add_field(name="Auteur", value=author.mention if author else f"<@{row['user_id']}>")
    embed.add_field(name="Statut", value=label)
    if row["response"]:
        embed.add_field(name="Réponse de l'équipe", value=row["response"], inline=False)
    return embed


class Community(commands.Cog):
    """Règlement, sondages et suggestions."""

    def __init__(self, bot):
        self.bot = bot
        self.last_suggestion = {}

    async def cog_load(self):
        self.bot.add_view(RulesView())

    # ----- Règlement -----
    @app_commands.command(name="reglement", description="Poster le règlement avec un bouton d'acceptation")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reglement(self, interaction: discord.Interaction, salon: Optional[discord.TextChannel] = None):
        await interaction.response.send_modal(RulesModal(salon or interaction.channel))

    # ----- Sondages -----
    @app_commands.command(name="sondage", description="Créer un sondage (réactions)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.describe(question="La question du sondage", option1="Laisse les options vides pour un sondage Oui/Non")
    async def sondage(
        self,
        interaction: discord.Interaction,
        question: str,
        option1: Optional[str] = None,
        option2: Optional[str] = None,
        option3: Optional[str] = None,
        option4: Optional[str] = None,
        option5: Optional[str] = None,
    ):
        options = [o for o in (option1, option2, option3, option4, option5) if o]
        if len(options) == 1:
            return await interaction.response.send_message(
                embed=embeds.error("Il faut au moins 2 options (ou aucune pour un sondage Oui/Non)."), ephemeral=True
            )
        if options:
            emojis = NUMBER_EMOJIS[: len(options)]
            body = "\n".join(f"{e} {o}" for e, o in zip(emojis, options))
        else:
            emojis = ["✅", "❌"]
            body = "✅ Oui\n❌ Non"

        embed = embeds.make(f"📊 {question}"[:256], body, "info", footer=f"Sondage lancé par {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()
        for emoji in emojis:
            await message.add_reaction(emoji)

    # ----- Suggestions -----
    @app_commands.command(name="suggestion", description="Proposer une idée à l'équipe")
    @app_commands.guild_only()
    async def suggestion(self, interaction: discord.Interaction, texte: app_commands.Range[str, 5, 1000]):
        channel_id = db.get_int(interaction.guild_id, "suggestion_channel")
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return await interaction.response.send_message(
                embed=embeds.error("Les suggestions ne sont pas activées sur ce serveur."), ephemeral=True
            )
        key = (interaction.guild_id, interaction.user.id)
        wait = 60 - (time.monotonic() - self.last_suggestion.get(key, -999))
        if wait > 0:
            return await interaction.response.send_message(
                embed=embeds.error(f"Attends encore {int(wait) + 1} secondes avant une nouvelle suggestion."), ephemeral=True
            )
        self.last_suggestion[key] = time.monotonic()

        await interaction.response.defer(ephemeral=True)
        suggestion_id, _ = db.run(
            "INSERT INTO suggestions (guild_id, channel_id, user_id, content) VALUES (?, ?, ?, ?)",
            (interaction.guild_id, channel.id, interaction.user.id, texte),
        )
        row = db.one("SELECT * FROM suggestions WHERE id=?", (suggestion_id,))
        try:
            message = await channel.send(embed=suggestion_embed(row, interaction.guild))
            await message.add_reaction("👍")
            await message.add_reaction("👎")
        except discord.HTTPException:
            return await interaction.followup.send(
                embed=embeds.error("Impossible de publier ta suggestion pour le moment."), ephemeral=True
            )
        db.run("UPDATE suggestions SET message_id=? WHERE id=?", (message.id, suggestion_id))
        await interaction.followup.send(embed=embeds.success(f"Ta suggestion #{suggestion_id} a été publiée !"), ephemeral=True)

    @app_commands.command(name="suggestion-reponse", description="Répondre à une suggestion")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.choices(statut=SUGGESTION_CHOICES)
    async def suggestion_reponse(
        self,
        interaction: discord.Interaction,
        suggestion_id: int,
        statut: app_commands.Choice[str],
        reponse: Optional[str] = None,
    ):
        row = db.one("SELECT * FROM suggestions WHERE id=? AND guild_id=?", (suggestion_id, interaction.guild_id))
        if row is None:
            return await interaction.response.send_message(embed=embeds.error("Suggestion introuvable."), ephemeral=True)
        db.run("UPDATE suggestions SET status=?, response=? WHERE id=?", (statut.value, reponse, suggestion_id))
        row = db.one("SELECT * FROM suggestions WHERE id=?", (suggestion_id,))

        channel = interaction.guild.get_channel(row["channel_id"])
        try:
            message = await channel.fetch_message(row["message_id"])
            await message.edit(embed=suggestion_embed(row, interaction.guild))
        except (discord.HTTPException, AttributeError):
            return await interaction.response.send_message(
                embed=embeds.error("Statut enregistré, mais je n'ai pas pu modifier le message de la suggestion."),
                ephemeral=True,
            )
        await interaction.response.send_message(embed=embeds.success("Suggestion mise à jour."), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Community(bot))
