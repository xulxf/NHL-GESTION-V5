import datetime
import os
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.tickets import is_staff, set_field
from utils import db, embeds, status
from utils.helpers import fmt_duration
from utils.logs import send_log

STATUS_CHOICES = [
    app_commands.Choice(name=f"{emoji} {name}", value=key)
    for key, (emoji, name) in status.STATUSES.items()
]


async def _to_file(att: discord.Attachment, base: str) -> discord.File:
    ext = os.path.splitext(att.filename)[1].lower()
    return await att.to_file(filename=f"{base}{ext}")


class Orders(commands.Cog):
    """Suivi de commande, ouverture/fermeture des commandes, vitrine et statistiques."""

    def __init__(self, bot):
        self.bot = bot

    # ---------- Statut d'une commande ----------
    @app_commands.command(name="statut", description="Changer le statut de la commande (à utiliser dans un ticket)")
    @app_commands.guild_only()
    @app_commands.choices(statut=STATUS_CHOICES)
    async def statut(self, interaction: discord.Interaction, statut: app_commands.Choice[str]):
        if not is_staff(interaction.user):
            return await interaction.response.send_message(embed=embeds.error("Réservé à l'équipe."), ephemeral=True)
        row = db.one("SELECT * FROM tickets WHERE channel_id=? AND closed_at IS NULL", (interaction.channel_id,))
        if row is None:
            return await interaction.response.send_message(
                embed=embeds.error("Utilise cette commande dans un ticket ouvert."), ephemeral=True
            )

        await interaction.response.defer()
        guild, key = interaction.guild, statut.value
        label = status.label(key)
        db.run("UPDATE tickets SET status=? WHERE id=?", (key, row["id"]))

        # Met à jour l'embed principal du ticket
        if row["message_id"]:
            try:
                message = await interaction.channel.fetch_message(row["message_id"])
                embed = message.embeds[0]
                set_field(embed, "Statut", label)
                await message.edit(embed=embed)
            except (discord.HTTPException, IndexError):
                pass

        done = key in ("terminee", "livree")
        update = embeds.make(
            "📦 Mise à jour de ta commande",
            f"Nouveau statut : **{label}**",
            "success" if done else "info",
            section="ticket",
        )
        await interaction.followup.send(content=f"<@{row['user_id']}>", embed=update)

        member = guild.get_member(row["user_id"])
        if member and done:
            try:
                await member.send(
                    embed=embeds.make(
                        "📦 Ta commande a avancé",
                        f"Ta commande sur **{guild.name}** est maintenant : **{label}**.",
                        "success",
                        section="ticket",
                    )
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        # Rôle « Client » une fois la commande livrée
        if key == "livree" and member:
            role_id = db.get_int(guild.id, "client_role")
            role = guild.get_role(role_id) if role_id else None
            if role:
                try:
                    await member.add_roles(role, reason="Commande livrée")
                except discord.HTTPException:
                    pass

        log = embeds.make("📦 Statut de commande", None, "info")
        log.add_field(name="Ticket", value=interaction.channel.mention)
        log.add_field(name="Statut", value=label)
        log.add_field(name="Par", value=interaction.user.mention)
        await send_log(guild, log)

    # ---------- Ouverture / fermeture des commandes ----------
    commandes = app_commands.Group(
        name="commandes",
        description="Ouvrir ou fermer les commandes",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @commandes.command(name="ouvrir", description="Ouvrir les commandes")
    async def ouvrir(self, interaction: discord.Interaction):
        db.set_config(interaction.guild_id, "orders_open", "1")
        await interaction.response.send_message(embed=embeds.success("Les commandes sont **ouvertes**."), ephemeral=True)

    @commandes.command(name="fermer", description="Fermer les commandes")
    @app_commands.describe(message="Message montré aux clients (facultatif)")
    async def fermer(self, interaction: discord.Interaction, message: Optional[str] = None):
        db.set_config(interaction.guild_id, "orders_open", "0")
        if message:
            db.set_config(interaction.guild_id, "orders_closed_msg", message)
        await interaction.response.send_message(
            embed=embeds.success("Les commandes sont **fermées**. Les clients ne peuvent plus en passer."), ephemeral=True
        )

    # ---------- Vitrine ----------
    @app_commands.command(name="realisation", description="Publier une réalisation dans la vitrine")
    @app_commands.guild_only()
    @app_commands.describe(
        nom="Nom de la réalisation",
        arme="Arme (facultatif)",
        couleur="Couleur (facultatif)",
        effet="Effet (facultatif)",
        client="Client concerné (facultatif)",
        image="Image ou GIF",
        video="Vidéo",
    )
    async def realisation(
        self,
        interaction: discord.Interaction,
        nom: str,
        arme: Optional[str] = None,
        couleur: Optional[str] = None,
        effet: Optional[str] = None,
        client: Optional[discord.Member] = None,
        image: Optional[discord.Attachment] = None,
        video: Optional[discord.Attachment] = None,
    ):
        if not is_staff(interaction.user):
            return await interaction.response.send_message(embed=embeds.error("Réservé à l'équipe."), ephemeral=True)
        guild = interaction.guild
        channel_id = db.get_int(guild.id, "showcase_channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return await interaction.response.send_message(
                embed=embeds.error("Aucun salon de vitrine : utilise `/setup vitrine #salon`."), ephemeral=True
            )
        limit = guild.filesize_limit
        for att, kind in ((image, "image"), (video, "video")):
            if att is None:
                continue
            if not (att.content_type or "").startswith(f"{kind}/"):
                return await interaction.response.send_message(
                    embed=embeds.error(f"« {att.filename} » n'est pas une {kind} valide."), ephemeral=True
                )
            if att.size > limit:
                return await interaction.response.send_message(
                    embed=embeds.error(f"« {att.filename} » dépasse la limite du serveur ({limit // 1048576} Mo)."),
                    ephemeral=True,
                )

        await interaction.response.defer(ephemeral=True)
        embed = embeds.make(f"✨ {nom}", None, "success", section="ticket")
        if arme:
            embed.add_field(name="⚔️ Arme", value=arme)
        if couleur:
            embed.add_field(name="🎨 Couleur", value=couleur)
        if effet:
            embed.add_field(name="✨ Effet", value=effet)
        if client:
            embed.add_field(name="👤 Client", value=client.mention)

        files = []
        try:
            if image:
                image_file = await _to_file(image, "image")
                embed.set_image(url=f"attachment://{image_file.filename}")
                files.append(image_file)
            if video:
                files.append(await _to_file(video, "video"))
            sent = await channel.send(embed=embed, files=files)
        except discord.HTTPException:
            return await interaction.followup.send(
                embed=embeds.error("Impossible d'envoyer la réalisation (fichier trop lourd ou permissions manquantes)."),
                ephemeral=True,
            )
        await interaction.followup.send(embed=embeds.success(f"Réalisation publiée : {sent.jump_url}"), ephemeral=True)

    # ---------- Statistiques ----------
    @app_commands.command(name="stats", description="Statistiques des tickets et des commandes")
    @app_commands.guild_only()
    async def stats(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message(embed=embeds.error("Réservé à l'équipe."), ephemeral=True)
        gid = interaction.guild_id
        month_start = datetime.datetime.now(datetime.timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        ).timestamp()

        total = db.one("SELECT COUNT(*) AS c FROM tickets WHERE guild_id=?", (gid,))["c"]
        opened = db.one("SELECT COUNT(*) AS c FROM tickets WHERE guild_id=? AND closed_at IS NULL", (gid,))["c"]
        this_month = db.one("SELECT COUNT(*) AS c FROM tickets WHERE guild_id=? AND created_at>=?", (gid, month_start))["c"]
        resp = db.one(
            "SELECT AVG(first_response_at - created_at) AS a FROM tickets WHERE guild_id=? AND first_response_at IS NOT NULL",
            (gid,),
        )["a"]
        reviews = db.one("SELECT AVG(rating) AS a, COUNT(*) AS c FROM reviews WHERE guild_id=?", (gid,))
        by_status = {
            r["status"]: r["c"]
            for r in db.rows("SELECT status, COUNT(*) AS c FROM tickets WHERE guild_id=? GROUP BY status", (gid,))
        }
        top = db.rows(
            "SELECT claimed_by, COUNT(*) AS c FROM tickets WHERE guild_id=? AND claimed_by IS NOT NULL "
            "GROUP BY claimed_by ORDER BY c DESC LIMIT 3",
            (gid,),
        )

        embed = embeds.make("📊 Statistiques", None, "info")
        embed.add_field(
            name="🎫 Tickets",
            value=f"Total : **{total}**\nOuverts : **{opened}**\nFermés : **{total - opened}**\nCe mois-ci : **{this_month}**",
        )
        embed.add_field(
            name="📦 Commandes",
            value="\n".join(f"{status.label(k)} : **{by_status.get(k, 0)}**" for k in status.STATUSES),
        )
        embed.add_field(
            name="⏱️ Première réponse",
            value=f"**{fmt_duration(resp)}** en moyenne" if resp is not None else "*Pas encore de données*",
        )
        if reviews["c"]:
            embed.add_field(name="⭐ Avis", value=f"**{reviews['a']:.1f}/5** sur {reviews['c']} avis")
        else:
            embed.add_field(name="⭐ Avis", value="*Aucun avis pour l'instant*")
        if top:
            embed.add_field(
                name="🏆 Équipe (tickets pris en charge)",
                value="\n".join(f"<@{r['claimed_by']}> : **{r['c']}**" for r in top),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Orders(bot))
