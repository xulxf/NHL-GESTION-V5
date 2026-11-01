from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import db, embeds
from utils.logs import send_log

Reason = app_commands.Range[str, 1, 400]
DEFAULT_REASON = "Aucune raison fournie"

WARN_TIMEOUT_EVERY = 3     # sourdine automatique tous les 3 avertissements
WARN_TIMEOUT_MINUTES = 60


def check_target(interaction: discord.Interaction, member: discord.Member) -> Optional[str]:
    """Retourne un message d'erreur si la sanction est impossible, sinon None."""
    guild = interaction.guild
    if member.id == interaction.user.id:
        return "Tu ne peux pas te sanctionner toi-même."
    if member.id == interaction.client.user.id:
        return "Je ne vais pas me sanctionner moi-même 😄"
    if member.id == guild.owner_id:
        return "Impossible de sanctionner le propriétaire du serveur."
    if interaction.user.id != guild.owner_id and member.top_role >= interaction.user.top_role:
        return "Ce membre a un rôle supérieur ou égal au tien."
    if member.top_role >= guild.me.top_role:
        return "Mon rôle est trop bas pour agir sur ce membre."
    return None


async def dm_user(member, title, guild, reason):
    try:
        await member.send(
            embed=embeds.make(title, f"**Serveur :** {guild.name}\n**Raison :** {reason}", "mod", section="mod")
        )
    except (discord.Forbidden, discord.HTTPException):
        pass


def mod_embed(action, target, moderator, reason, extra=None) -> discord.Embed:
    embed = embeds.make(f"🔨 {action}", None, "mod", section="mod")
    embed.add_field(name="Membre", value=f"{target.mention}\n`{target.id}`")
    embed.add_field(name="Modérateur", value=moderator.mention)
    if extra:
        embed.add_field(name=extra[0], value=extra[1])
    embed.add_field(name="Raison", value=reason, inline=False)
    embed.set_thumbnail(url=target.display_avatar.url)
    return embed


async def finish(interaction: discord.Interaction, embed: discord.Embed):
    """Répond publiquement et journalise."""
    await interaction.followup.send(embed=embed)
    await send_log(interaction.guild, embed)


async def refuse(interaction: discord.Interaction, text: str):
    await interaction.response.send_message(embed=embeds.error(text), ephemeral=True)


class Moderation(commands.Cog):
    """Commandes de modération."""

    def __init__(self, bot):
        self.bot = bot

    # ---------- Sanctions ----------
    @app_commands.command(name="kick", description="Expulser un membre")
    @app_commands.guild_only()
    @app_commands.default_permissions(kick_members=True)
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, membre: discord.Member, raison: Reason = DEFAULT_REASON):
        if problem := check_target(interaction, membre):
            return await refuse(interaction, problem)
        await interaction.response.defer()
        await dm_user(membre, "👢 Tu as été expulsé", interaction.guild, raison)
        await membre.kick(reason=f"{interaction.user} : {raison}")
        await finish(interaction, mod_embed("Expulsion", membre, interaction.user, raison))

    @app_commands.command(name="ban", description="Bannir un membre")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @app_commands.describe(jours="Jours de messages à supprimer (0 à 7)")
    async def ban(
        self,
        interaction: discord.Interaction,
        membre: discord.Member,
        raison: Reason = DEFAULT_REASON,
        jours: app_commands.Range[int, 0, 7] = 0,
    ):
        if problem := check_target(interaction, membre):
            return await refuse(interaction, problem)
        await interaction.response.defer()
        await dm_user(membre, "🔨 Tu as été banni", interaction.guild, raison)
        await membre.ban(reason=f"{interaction.user} : {raison}", delete_message_seconds=jours * 86400)
        await finish(interaction, mod_embed("Bannissement", membre, interaction.user, raison))

    @app_commands.command(name="unban", description="Débannir un utilisateur via son ID")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, utilisateur_id: str, raison: Reason = DEFAULT_REASON):
        await interaction.response.defer()
        try:
            user = await self.bot.fetch_user(int(utilisateur_id))
            await interaction.guild.unban(user, reason=f"{interaction.user} : {raison}")
        except (ValueError, discord.NotFound):
            return await interaction.followup.send(
                embed=embeds.error("Utilisateur introuvable ou non banni."), ephemeral=True
            )
        await finish(interaction, mod_embed("Débannissement", user, interaction.user, raison))

    @app_commands.command(name="timeout", description="Mettre un membre en sourdine")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    @app_commands.describe(duree="Durée en minutes (max 28 jours)")
    async def timeout(
        self,
        interaction: discord.Interaction,
        membre: discord.Member,
        duree: app_commands.Range[int, 1, 40320],
        raison: Reason = DEFAULT_REASON,
    ):
        if problem := check_target(interaction, membre):
            return await refuse(interaction, problem)
        await interaction.response.defer()
        until = discord.utils.utcnow() + timedelta(minutes=duree)
        await membre.timeout(until - discord.utils.utcnow(), reason=f"{interaction.user} : {raison}")
        await dm_user(membre, "🔇 Tu as été mis en sourdine", interaction.guild, raison)
        embed = mod_embed(
            "Sourdine", membre, interaction.user, raison,
            ("Fin", discord.utils.format_dt(until, "R")),
        )
        await finish(interaction, embed)

    @app_commands.command(name="untimeout", description="Lever la sourdine d'un membre")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction, membre: discord.Member, raison: Reason = DEFAULT_REASON):
        if problem := check_target(interaction, membre):
            return await refuse(interaction, problem)
        await interaction.response.defer()
        await membre.timeout(None, reason=f"{interaction.user} : {raison}")
        await finish(interaction, mod_embed("Fin de sourdine", membre, interaction.user, raison))

    # ---------- Avertissements ----------
    @app_commands.command(name="warn", description="Avertir un membre")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(self, interaction: discord.Interaction, membre: discord.Member, raison: Reason):
        if problem := check_target(interaction, membre):
            return await refuse(interaction, problem)
        await interaction.response.defer()
        guild = interaction.guild
        warn_id = db.add_warn(guild.id, membre.id, interaction.user.id, raison)
        total = len(db.get_warns(guild.id, membre.id))

        embed = mod_embed(
            "Avertissement", membre, interaction.user, raison,
            ("Total", f"{total} avertissement(s) • ID `#{warn_id}`"),
        )
        await dm_user(membre, "⚠️ Tu as reçu un avertissement", guild, raison)

        if total % WARN_TIMEOUT_EVERY == 0:
            try:
                await membre.timeout(
                    timedelta(minutes=WARN_TIMEOUT_MINUTES),
                    reason=f"{total} avertissements",
                )
                embed.add_field(
                    name="Sanction automatique",
                    value=f"Sourdine de {WARN_TIMEOUT_MINUTES} min ({total} avertissements)",
                    inline=False,
                )
            except discord.HTTPException:
                pass
        await finish(interaction, embed)

    @app_commands.command(name="warnings", description="Voir les avertissements d'un membre")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings(self, interaction: discord.Interaction, membre: discord.Member):
        rows = db.get_warns(interaction.guild_id, membre.id)
        if not rows:
            return await interaction.response.send_message(
                embed=embeds.info(f"{membre.mention} n'a aucun avertissement."), ephemeral=True
            )
        lines = [
            f"`#{r['id']}` • <t:{r['created_at']}:d> • <@{r['mod_id']}>\n└ {r['reason']}"
            for r in rows[-10:]
        ]
        embed = embeds.make(
            f"⚠️ Avertissements de {membre.display_name}",
            "\n".join(lines),
            "warn",
            footer=f"{len(rows)} au total • les 10 derniers sont affichés",
            section="mod",
        )
        embed.set_thumbnail(url=membre.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="delwarn", description="Supprimer un avertissement via son ID")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def delwarn(self, interaction: discord.Interaction, warn_id: int):
        if db.delete_warn(interaction.guild_id, warn_id):
            await interaction.response.send_message(
                embed=embeds.success(f"Avertissement `#{warn_id}` supprimé."), ephemeral=True
            )
        else:
            await refuse(interaction, "Avertissement introuvable.")

    @app_commands.command(name="clearwarns", description="Effacer tous les avertissements d'un membre")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def clearwarns(self, interaction: discord.Interaction, membre: discord.Member):
        count = db.clear_warns(interaction.guild_id, membre.id)
        await interaction.response.send_message(
            embed=embeds.success(f"{count} avertissement(s) supprimé(s) pour {membre.mention}."),
            ephemeral=True,
        )

    # ---------- Gestion des salons ----------
    @app_commands.command(name="clear", description="Supprimer des messages")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True, read_message_history=True)
    async def clear(
        self,
        interaction: discord.Interaction,
        nombre: app_commands.Range[int, 1, 100],
        membre: Optional[discord.Member] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        kwargs = {"check": lambda m: m.author.id == membre.id} if membre else {}
        deleted = await interaction.channel.purge(limit=nombre, **kwargs)
        await interaction.followup.send(
            embed=embeds.success(f"{len(deleted)} message(s) supprimé(s)."), ephemeral=True
        )
        log = embeds.make("🧹 Nettoyage", None, "warn")
        log.add_field(name="Modérateur", value=interaction.user.mention)
        log.add_field(name="Salon", value=interaction.channel.mention)
        log.add_field(name="Messages", value=str(len(deleted)))
        await send_log(interaction.guild, log)

    @app_commands.command(name="slowmode", description="Définir le mode lent du salon")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @app_commands.describe(secondes="0 pour désactiver (max 6 h)")
    async def slowmode(self, interaction: discord.Interaction, secondes: app_commands.Range[int, 0, 21600]):
        await interaction.channel.edit(slowmode_delay=secondes)
        text = f"Mode lent réglé sur **{secondes}s**." if secondes else "Mode lent désactivé."
        await interaction.response.send_message(embed=embeds.success(text))

    @app_commands.command(name="lock", description="Verrouiller le salon")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def lock(self, interaction: discord.Interaction):
        everyone = interaction.guild.default_role
        overwrite = interaction.channel.overwrites_for(everyone)
        overwrite.send_messages = False
        await interaction.channel.set_permissions(everyone, overwrite=overwrite)
        await interaction.response.send_message(embed=embeds.make("🔒 Salon verrouillé", "Seule l'équipe peut écrire ici.", "warn"))

    @app_commands.command(name="unlock", description="Déverrouiller le salon")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction):
        everyone = interaction.guild.default_role
        overwrite = interaction.channel.overwrites_for(everyone)
        overwrite.send_messages = None
        await interaction.channel.set_permissions(everyone, overwrite=overwrite)
        await interaction.response.send_message(embed=embeds.make("🔓 Salon déverrouillé", "Tout le monde peut de nouveau écrire.", "success"))


async def setup(bot):
    await bot.add_cog(Moderation(bot))
