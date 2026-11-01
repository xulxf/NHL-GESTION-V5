import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import db, embeds
from utils.helpers import level_progress

XP_COOLDOWN = 60  # secondes entre deux gains d'XP pour un même membre


class Levels(commands.Cog):
    """Niveaux et XP selon l'activité dans les salons."""

    def __init__(self, bot):
        self.bot = bot
        self.cooldowns = {}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        guild_id = message.guild.id
        if db.get_config(guild_id, "levels_enabled", "1") != "1":
            return
        key = (guild_id, message.author.id)
        now = time.monotonic()
        if now - self.cooldowns.get(key, -9999) < XP_COOLDOWN:
            return
        self.cooldowns[key] = now

        row = db.one("SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?", key)
        old_level = row["level"] if row else 0
        xp = (row["xp"] if row else 0) + random.randint(15, 25)
        level = level_progress(xp)[0]
        db.run(
            "INSERT INTO levels (guild_id, user_id, xp, level) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp=excluded.xp, level=excluded.level",
            (guild_id, message.author.id, xp, level),
        )
        if level <= old_level:
            return

        try:
            await message.channel.send(
                embed=embeds.make("🎉 Niveau supérieur !", f"{message.author.mention} passe **niveau {level}** !", "success")
            )
        except discord.HTTPException:
            pass

        rewards = db.rows("SELECT role_id FROM level_roles WHERE guild_id=? AND level<=?", (guild_id, level))
        roles = [message.guild.get_role(r["role_id"]) for r in rewards]
        missing = [r for r in roles if r and r not in message.author.roles]
        if missing:
            try:
                await message.author.add_roles(*missing, reason=f"Niveau {level}")
            except discord.HTTPException:
                pass

    # ---------- Commandes publiques ----------
    @app_commands.command(name="rank", description="Voir ton niveau et ton XP")
    @app_commands.guild_only()
    async def rank(self, interaction: discord.Interaction, membre: Optional[discord.Member] = None):
        membre = membre or interaction.user
        row = db.one("SELECT xp FROM levels WHERE guild_id=? AND user_id=?", (interaction.guild_id, membre.id))
        xp = row["xp"] if row else 0
        level, current, needed = level_progress(xp)
        position = db.one("SELECT COUNT(*) + 1 AS p FROM levels WHERE guild_id=? AND xp>?", (interaction.guild_id, xp))["p"]
        filled = int(10 * current / needed)
        bar = "█" * filled + "░" * (10 - filled)

        embed = embeds.make(f"📈 {membre.display_name}", None, "info")
        embed.add_field(name="Niveau", value=f"**{level}**")
        embed.add_field(name="Classement", value=f"**#{position}**")
        embed.add_field(name="XP total", value=f"**{xp}**")
        embed.add_field(name=f"Progression ({current}/{needed} XP)", value=bar, inline=False)
        embed.set_thumbnail(url=membre.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="classement", description="Top 10 des membres les plus actifs")
    @app_commands.guild_only()
    async def classement(self, interaction: discord.Interaction):
        top = db.rows("SELECT user_id, xp FROM levels WHERE guild_id=? ORDER BY xp DESC LIMIT 10", (interaction.guild_id,))
        if not top:
            return await interaction.response.send_message(embed=embeds.info("Personne n'a encore d'XP."), ephemeral=True)
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, r in enumerate(top):
            prefix = medals[i] if i < 3 else f"**{i + 1}.**"
            lines.append(f"{prefix} <@{r['user_id']}> — niveau {level_progress(r['xp'])[0]} ({r['xp']} XP)")
        await interaction.response.send_message(embed=embeds.make("🏆 Classement", "\n".join(lines), "info"))

    # ---------- Administration ----------
    niveaux = app_commands.Group(
        name="niveaux",
        description="Configurer les niveaux",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @niveaux.command(name="activer", description="Activer ou désactiver le système de niveaux")
    async def activer(self, interaction: discord.Interaction, etat: bool):
        db.set_config(interaction.guild_id, "levels_enabled", "1" if etat else "0")
        await interaction.response.send_message(
            embed=embeds.success("Niveaux **activés**." if etat else "Niveaux **désactivés**."), ephemeral=True
        )

    @niveaux.command(name="recompense", description="Donner un rôle à partir d'un niveau")
    async def recompense(self, interaction: discord.Interaction, niveau: app_commands.Range[int, 1, 200], role: discord.Role):
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(
                embed=embeds.error("Ce rôle est au-dessus du mien, je ne pourrai pas le donner."), ephemeral=True
            )
        db.run(
            "INSERT INTO level_roles (guild_id, level, role_id) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, level) DO UPDATE SET role_id=excluded.role_id",
            (interaction.guild_id, niveau, role.id),
        )
        await interaction.response.send_message(
            embed=embeds.success(f"Niveau **{niveau}** : {role.mention}"), ephemeral=True
        )

    @niveaux.command(name="retirer", description="Retirer la récompense d'un niveau")
    async def retirer(self, interaction: discord.Interaction, niveau: int):
        _, count = db.run("DELETE FROM level_roles WHERE guild_id=? AND level=?", (interaction.guild_id, niveau))
        text = "Récompense retirée." if count else "Aucune récompense à ce niveau."
        await interaction.response.send_message(embed=embeds.info(text), ephemeral=True)

    @niveaux.command(name="liste", description="Voir les récompenses de niveaux")
    async def liste(self, interaction: discord.Interaction):
        rows = db.rows("SELECT level, role_id FROM level_roles WHERE guild_id=? ORDER BY level", (interaction.guild_id,))
        if not rows:
            return await interaction.response.send_message(embed=embeds.info("Aucune récompense configurée."), ephemeral=True)
        lines = [f"Niveau **{r['level']}** → <@&{r['role_id']}>" for r in rows]
        await interaction.response.send_message(embed=embeds.make("🎁 Récompenses", "\n".join(lines), "info"), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Levels(bot))
