"""Outils d'équipe dans les tickets : devis, paiement, priorité et notes internes."""
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.tickets import is_staff, set_field
from utils import db, embeds, status

PRIORITIES = {"urgent": "🔴 Urgent", "normal": "🟢 Normal", "bas": "🔵 Basse"}


def euros(cents: int) -> str:
    return f"{cents / 100:.2f} €".replace(".", ",")


def quote_summary(row) -> str:
    if row["price_cents"] is None:
        return "—"
    state = {"sent": "⏳ en attente de réponse", "accepted": "✅ accepté", "refused": "❌ refusé"}.get(row["quote_status"], "")
    paid = "💳 payé" if row["paid"] else ""
    return " · ".join(part for part in (euros(row["price_cents"]), state, paid) if part)


def ensure_field(embed: discord.Embed, key: str, name: str, value: str, inline: bool = True):
    """Modifie le champ dont le nom contient `key`, ou l'ajoute s'il n'existe pas."""
    for i, field in enumerate(embed.fields):
        if key in field.name:
            embed.set_field_at(i, name=field.name, value=value, inline=field.inline)
            return
    embed.add_field(name=name, value=value, inline=inline)


async def refresh_ticket_embed(channel, row):
    """Met à jour l'embed principal du ticket (devis, priorité)."""
    if not row["message_id"]:
        return
    try:
        message = await channel.fetch_message(row["message_id"])
        embed = message.embeds[0]
        ensure_field(embed, "Devis", "💶 Devis", quote_summary(row))
        ensure_field(embed, "Priorité", "🚦 Priorité", PRIORITIES.get(row["priority"] or "normal", PRIORITIES["normal"]))
        await message.edit(embed=embed)
    except (discord.HTTPException, IndexError):
        pass


def ticket_row(channel_id):
    return db.one("SELECT * FROM tickets WHERE channel_id=? AND closed_at IS NULL", (channel_id,))


async def answer_quote(interaction: discord.Interaction, decision: str):
    row = ticket_row(interaction.channel_id)
    if row is None:
        return await interaction.response.send_message(embed=embeds.error("Ce salon n'est pas un ticket ouvert."), ephemeral=True)
    if interaction.user.id != row["user_id"]:
        return await interaction.response.send_message(embed=embeds.error("Seul le client peut répondre à ce devis."), ephemeral=True)
    if row["quote_status"] != "sent":
        return await interaction.response.send_message(embed=embeds.error("Ce devis a déjà reçu une réponse."), ephemeral=True)

    db.run("UPDATE tickets SET quote_status=? WHERE id=?", (decision, row["id"]))
    accepted = decision == "accepted"
    if accepted and row["status"] == "attente":
        db.run("UPDATE tickets SET status='cours' WHERE id=?", (row["id"],))
    row = db.one("SELECT * FROM tickets WHERE id=?", (row["id"],))

    verdict = embeds.make(
        "✅ Devis accepté" if accepted else "❌ Devis refusé",
        f"Montant : **{euros(row['price_cents'])}**",
        "success" if accepted else "error",
    )
    await interaction.response.edit_message(embed=verdict, view=None)

    staff_id = db.get_int(interaction.guild_id, "staff_role")
    who = f"<@{row['claimed_by']}>" if row["claimed_by"] else (f"<@&{staff_id}>" if staff_id else None)
    await interaction.channel.send(
        content=who,
        embed=embeds.make(
            "Réponse du client",
            f"{interaction.user.mention} a **{'accepté' if accepted else 'refusé'}** le devis de {euros(row['price_cents'])}.",
            "success" if accepted else "warn",
        ),
    )
    if accepted:  # le statut passe à « En cours » dans l'embed principal
        try:
            message = await interaction.channel.fetch_message(row["message_id"])
            embed = message.embeds[0]
            set_field(embed, "Statut", status.label("cours"))
            await message.edit(embed=embed)
        except (discord.HTTPException, IndexError, TypeError):
            pass
    await refresh_ticket_embed(interaction.channel, row)


class QuoteView(discord.ui.View):
    """Boutons du devis (persistants : ils marchent après un redémarrage)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="J'accepte le devis", emoji="✅", style=discord.ButtonStyle.success, custom_id="quote:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await answer_quote(interaction, "accepted")

    @discord.ui.button(label="Refuser", emoji="❌", style=discord.ButtonStyle.danger, custom_id="quote:refuse")
    async def refuse(self, interaction: discord.Interaction, button: discord.ui.Button):
        await answer_quote(interaction, "refused")


class TicketTools(commands.Cog):
    """Devis, paiement, priorité et notes internes."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(QuoteView())

    async def staff_ticket(self, interaction: discord.Interaction):
        """Retourne la ligne du ticket si la commande est utilisée par l'équipe dans un ticket ouvert."""
        if not is_staff(interaction.user):
            await interaction.response.send_message(embed=embeds.error("Réservé à l'équipe."), ephemeral=True)
            return None
        row = ticket_row(interaction.channel_id)
        if row is None:
            await interaction.response.send_message(embed=embeds.error("Utilise cette commande dans un ticket ouvert."), ephemeral=True)
        return row

    @app_commands.command(name="devis", description="Envoyer un devis au client (dans un ticket)")
    @app_commands.guild_only()
    @app_commands.describe(montant="Prix en euros", details="Précisions affichées sur le devis (facultatif)")
    async def devis(self, interaction: discord.Interaction, montant: app_commands.Range[float, 0.01, 100000], details: Optional[str] = None):
        row = await self.staff_ticket(interaction)
        if row is None:
            return
        cents = int(round(montant * 100))
        db.run("UPDATE tickets SET price_cents=?, quote_status='sent', paid=0 WHERE id=?", (cents, row["id"]))
        row = db.one("SELECT * FROM tickets WHERE id=?", (row["id"],))
        embed = embeds.make("💶 Devis", f"Montant proposé : **{euros(cents)}**", "info", section="ticket")
        if details:
            embed.add_field(name="Détails", value=details[:1000], inline=False)
        embed.set_footer(text="Clique sur un bouton pour répondre")
        await interaction.response.send_message(content=f"<@{row['user_id']}>", embed=embed, view=QuoteView())
        await refresh_ticket_embed(interaction.channel, row)

    @app_commands.command(name="paye", description="Marquer le ticket comme payé (ou annuler)")
    @app_commands.guild_only()
    @app_commands.describe(annuler="Retirer la mention « payé »")
    async def paye(self, interaction: discord.Interaction, annuler: bool = False):
        row = await self.staff_ticket(interaction)
        if row is None:
            return
        if row["price_cents"] is None:
            return await interaction.response.send_message(embed=embeds.error("Envoie d'abord un devis avec `/devis`."), ephemeral=True)
        db.run("UPDATE tickets SET paid=? WHERE id=?", (0 if annuler else 1, row["id"]))
        row = db.one("SELECT * FROM tickets WHERE id=?", (row["id"],))
        text = "La mention « payé » a été retirée." if annuler else f"Paiement de **{euros(row['price_cents'])}** reçu. Merci !"
        await interaction.response.send_message(embed=embeds.make("💳 Paiement", text, "info" if annuler else "success"))
        await refresh_ticket_embed(interaction.channel, row)

    @app_commands.command(name="priorite", description="Changer la priorité du ticket")
    @app_commands.guild_only()
    @app_commands.choices(niveau=[app_commands.Choice(name=label, value=key) for key, label in PRIORITIES.items()])
    async def priorite(self, interaction: discord.Interaction, niveau: app_commands.Choice[str]):
        row = await self.staff_ticket(interaction)
        if row is None:
            return
        db.run("UPDATE tickets SET priority=? WHERE id=?", (niveau.value, row["id"]))
        row = db.one("SELECT * FROM tickets WHERE id=?", (row["id"],))
        await interaction.response.send_message(embed=embeds.make("🚦 Priorité", f"Priorité : **{PRIORITIES[niveau.value]}**", "warn" if niveau.value == "urgent" else "info"))
        await refresh_ticket_embed(interaction.channel, row)
        name = interaction.channel.name
        wanted = ("🔴-" + name.removeprefix("🔴-")) if niveau.value == "urgent" else name.removeprefix("🔴-")
        if wanted != name:
            try:
                await interaction.channel.edit(name=wanted)
            except discord.HTTPException:
                pass

    @app_commands.command(name="note", description="Ajouter une note interne (invisible pour le client)")
    @app_commands.guild_only()
    async def note(self, interaction: discord.Interaction, texte: app_commands.Range[str, 1, 1000.0]):
        row = await self.staff_ticket(interaction)
        if row is None:
            return
        db.run(
            "INSERT INTO ticket_notes (guild_id, ticket_id, author_id, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (interaction.guild_id, row["id"], interaction.user.id, texte, int(time.time())),
        )
        await interaction.response.send_message(embed=embeds.success("Note enregistrée. Seule l'équipe peut la lire (`/notes`)."), ephemeral=True)

    @app_commands.command(name="notes", description="Lire les notes internes du ticket")
    @app_commands.guild_only()
    async def notes(self, interaction: discord.Interaction):
        row = await self.staff_ticket(interaction)
        if row is None:
            return
        items = db.rows("SELECT * FROM ticket_notes WHERE ticket_id=? ORDER BY id DESC LIMIT 10", (row["id"],))
        if not items:
            return await interaction.response.send_message(embed=embeds.info("Aucune note pour ce ticket."), ephemeral=True)
        lines = [f"<t:{n['created_at']}:d> <@{n['author_id']}> : {n['content']}" for n in items]
        await interaction.response.send_message(embed=embeds.make("🗒️ Notes internes", "\n".join(lines)[:4000], "info"), ephemeral=True)


async def setup(bot):
    await bot.add_cog(TicketTools(bot))
