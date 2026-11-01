import time

import discord
from discord.ext import commands

from utils import db, embeds
from utils.helpers import stars


async def submit_review(interaction: discord.Interaction, rating: int, guild_id: int, number: int, comment: str):
    guild = interaction.client.get_guild(guild_id)
    ticket = db.one("SELECT * FROM tickets WHERE guild_id=? AND number=?", (guild_id, number))
    if guild is None or ticket is None or ticket["user_id"] != interaction.user.id:
        return await interaction.response.send_message(
            embed=embeds.error("Impossible d'enregistrer cet avis."), ephemeral=True
        )
    if db.one("SELECT 1 FROM reviews WHERE guild_id=? AND ticket_number=?", (guild_id, number)):
        return await interaction.response.send_message(
            embed=embeds.error("Tu as déjà donné ton avis pour ce ticket."), ephemeral=True
        )

    db.run(
        "INSERT INTO reviews (guild_id, ticket_number, user_id, rating, comment, created_at) VALUES (?,?,?,?,?,?)",
        (guild_id, number, interaction.user.id, rating, comment or None, int(time.time())),
    )

    channel_id = db.get_int(guild_id, "review_channel")
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel:
        embed = embeds.make("⭐ Nouvel avis", comment or "*Pas de commentaire*", "success", section="ticket")
        embed.add_field(name="Note", value=f"{stars(rating)} ({rating}/5)")
        embed.add_field(name="Client", value=interaction.user.display_name)
        embed.add_field(name="Ticket", value=f"{ticket['category'].capitalize()} #{number:04d}")
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    await interaction.response.edit_message(
        embed=embeds.success("Merci pour ton avis, il nous aide beaucoup ! 💙", "Avis envoyé"), view=None
    )


class ReviewModal(discord.ui.Modal, title="Ton avis"):
    comment = discord.ui.TextInput(
        label="Un commentaire ? (facultatif)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, rating: int, guild_id: int, number: int):
        super().__init__()
        self.rating, self.guild_id, self.number = rating, guild_id, number

    async def on_submit(self, interaction: discord.Interaction):
        await submit_review(interaction, self.rating, self.guild_id, self.number, self.comment.value.strip())


class ReviewButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"review:(?P<rating>[1-5]):(?P<guild>[0-9]+):(?P<number>[0-9]+)",
):
    """Bouton de note, fonctionnel même après un redémarrage du bot."""

    def __init__(self, rating: int, guild_id: int, number: int):
        super().__init__(
            discord.ui.Button(
                label=str(rating),
                emoji="⭐",
                style=discord.ButtonStyle.secondary,
                custom_id=f"review:{rating}:{guild_id}:{number}",
            )
        )
        self.rating, self.guild_id, self.number = rating, guild_id, number

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["rating"]), int(match["guild"]), int(match["number"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ReviewModal(self.rating, self.guild_id, self.number))


async def send_review_request(guild: discord.Guild, user: discord.abc.User, number: int, category: str):
    """Envoie la demande d'avis en message privé (si un salon d'avis est configuré)."""
    if not db.get_int(guild.id, "review_channel"):
        return
    embed = embeds.make(
        "⭐ Ton avis compte !",
        f"Merci d'avoir fait appel à **{guild.name}**.\nComment s'est passée ta demande ? Note-la de 1 à 5 :",
        "info",
        section="ticket",
    )
    view = discord.ui.View(timeout=None)
    for rating in range(1, 6):
        view.add_item(ReviewButton(rating, guild.id, number))
    try:
        await user.send(embed=embed, view=view)
    except (discord.Forbidden, discord.HTTPException):
        pass


class Reviews(commands.Cog):
    """Avis clients envoyés après la fermeture d'un ticket."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_dynamic_items(ReviewButton)


async def setup(bot):
    await bot.add_cog(Reviews(bot))
