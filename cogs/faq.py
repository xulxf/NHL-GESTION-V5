import discord
from discord import app_commands
from discord.ext import commands

from utils import db, embeds


class FaqSelect(discord.ui.Select):
    def __init__(self, rows):
        options = [
            discord.SelectOption(label=row["question"][:100], value=str(row["id"]))
            for row in rows[:25]
        ]
        super().__init__(placeholder="Choisis ta question…", options=options)

    async def callback(self, interaction: discord.Interaction):
        row = db.one("SELECT * FROM faq WHERE id=? AND guild_id=?", (int(self.values[0]), interaction.guild_id))
        if row is None:
            return await interaction.response.send_message(
                embed=embeds.error("Cette question n'existe plus."), ephemeral=True
            )
        await interaction.response.send_message(
            embed=embeds.make(f"❓ {row['question']}"[:256], row["answer"], "info"), ephemeral=True
        )


async def send_faq(interaction: discord.Interaction):
    """Affiche le menu de la FAQ (réponse visible uniquement par la personne)."""
    rows = db.rows("SELECT * FROM faq WHERE guild_id=? ORDER BY id", (interaction.guild_id,))
    if not rows:
        return await interaction.response.send_message(
            embed=embeds.info("La FAQ est vide pour le moment. Ouvre un ticket si tu as une question !"),
            ephemeral=True,
        )
    view = discord.ui.View(timeout=180)
    view.add_item(FaqSelect(rows))
    await interaction.response.send_message(
        embed=embeds.make("❓ Questions fréquentes", "Choisis ta question dans le menu ci-dessous :", "info"),
        view=view,
        ephemeral=True,
    )


class FaqModal(discord.ui.Modal, title="Nouvelle question FAQ"):
    question = discord.ui.TextInput(label="Question", max_length=100)
    answer = discord.ui.TextInput(label="Réponse", style=discord.TextStyle.paragraph, max_length=1500)

    async def on_submit(self, interaction: discord.Interaction):
        faq_id, _ = db.run(
            "INSERT INTO faq (guild_id, question, answer) VALUES (?, ?, ?)",
            (interaction.guild_id, self.question.value.strip(), self.answer.value.strip()),
        )
        await interaction.response.send_message(
            embed=embeds.success(f"Question ajoutée à la FAQ (ID `{faq_id}`)."), ephemeral=True
        )


class Faq(commands.Cog):
    """FAQ consultable par les clients."""

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="faq", description="Voir les questions fréquentes")
    @app_commands.guild_only()
    async def faq(self, interaction: discord.Interaction):
        await send_faq(interaction)

    admin = app_commands.Group(
        name="faq-admin",
        description="Gérer la FAQ",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @admin.command(name="ajouter", description="Ajouter une question à la FAQ")
    async def ajouter(self, interaction: discord.Interaction):
        await interaction.response.send_modal(FaqModal())

    @admin.command(name="liste", description="Lister les questions de la FAQ")
    async def liste(self, interaction: discord.Interaction):
        rows = db.rows("SELECT * FROM faq WHERE guild_id=? ORDER BY id", (interaction.guild_id,))
        if not rows:
            return await interaction.response.send_message(embed=embeds.info("La FAQ est vide."), ephemeral=True)
        lines = [f"`{r['id']}` • {r['question']}" for r in rows]
        await interaction.response.send_message(
            embed=embeds.make("❓ FAQ du serveur", "\n".join(lines)[:4000], "info"), ephemeral=True
        )

    @admin.command(name="supprimer", description="Supprimer une question via son ID")
    async def supprimer(self, interaction: discord.Interaction, faq_id: int):
        _, count = db.run("DELETE FROM faq WHERE id=? AND guild_id=?", (faq_id, interaction.guild_id))
        if count:
            await interaction.response.send_message(embed=embeds.success("Question supprimée."), ephemeral=True)
        else:
            await interaction.response.send_message(embed=embeds.error("ID introuvable."), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Faq(bot))
