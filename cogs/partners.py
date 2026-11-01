"""Partenaires : un lien d'invitation par partenaire, suivi des arrivées et paliers.

Le bot compare le nombre d'utilisations de chaque invitation avant/après chaque arrivée pour savoir
quel lien a été utilisé. Il lui faut la permission « Gérer le serveur » (lire les invitations) et
« Créer une invitation ».
"""
import asyncio
import datetime
import logging
import time

import discord
from discord.ext import commands, tasks

from cogs.tickets import create_system_ticket, find_ticket
from utils import db, embeds
from utils.helpers import tier_for, used_invite
from utils.logs import send_log


BOARD_SQL = (
    "SELECT p.name, (SELECT COUNT(*) FROM partner_referrals r WHERE r.partner_id=p.id AND r.left_at IS NULL) AS active_count "
    "FROM partners p WHERE p.guild_id=? AND p.active=1 ORDER BY active_count DESC, p.id LIMIT 10"
)


class Partners(commands.Cog):
    """Liens partenaires, arrivées suivies et paliers."""

    def __init__(self, bot):
        self.bot = bot
        self.invites = {}  # guild_id -> {code: utilisations}, None si le bot ne peut pas lire les invitations
        self.locks = {}
        self._started = False
        self._tasks = set()

    def lock(self, guild_id):
        return self.locks.setdefault(guild_id, asyncio.Lock())

    # ---------- Photo des invitations ----------
    async def snapshot(self, guild):
        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            self.invites[guild.id] = None
            return
        self.invites[guild.id] = {invite.code: invite.uses or 0 for invite in invites}

    async def snapshot_all(self):
        for guild in self.bot.guilds:
            await self.snapshot(guild)

    async def cog_load(self):
        self.weekly_board.start()
        if self.bot.is_ready():
            self._started = True
            task = asyncio.create_task(self.snapshot_all())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def cog_unload(self):
        self.weekly_board.cancel()

    @tasks.loop(hours=1)
    async def weekly_board(self):
        """Poste le classement des partenaires une fois par semaine dans le salon choisi."""
        now = int(time.time())
        for guild in self.bot.guilds:
            channel = guild.get_channel(db.get_int(guild.id, "partner_board_channel") or 0)
            if channel is None or now - int(db.get_config(guild.id, "partner_board_last", "0") or 0) < 7 * 86400:
                continue
            rows = db.rows(BOARD_SQL, (guild.id,))
            if not rows:
                continue
            db.set_config(guild.id, "partner_board_last", now)
            medals = ["🥇", "🥈", "🥉"]
            lines = [
                f"{medals[i] if i < 3 else f'**{i + 1}.**'} {r['name']} : **{int(r['active_count'] or 0)}** membre(s)"
                for i, r in enumerate(rows)
            ]
            try:
                await channel.send(embed=embeds.make("🏆 Classement des partenaires", "\n".join(lines), "success"))
            except discord.HTTPException:
                logging.warning("Impossible de publier le classement des partenaires")

    @weekly_board.before_loop
    async def before_board(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self._started:
            self._started = True
            await self.snapshot_all()

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        await self.snapshot(guild)

    @commands.Cog.listener()
    async def on_invite_create(self, invite):
        cache = self.invites.get(invite.guild.id) if invite.guild else None
        if cache is not None:
            cache[invite.code] = invite.uses or 0

    @commands.Cog.listener()
    async def on_invite_delete(self, invite):
        cache = self.invites.get(invite.guild.id) if invite.guild else None
        if cache is not None:
            cache.pop(invite.code, None)

    # ---------- Arrivées et départs ----------
    @commands.Cog.listener()
    async def on_member_join(self, member):
        if member.bot:
            return
        guild = member.guild
        async with self.lock(guild.id):
            old = self.invites.get(guild.id)
            try:
                invites = await guild.invites()
            except (discord.Forbidden, discord.HTTPException):
                self.invites[guild.id] = None
                return
            new = {invite.code: invite.uses or 0 for invite in invites}
            self.invites[guild.id] = new
            if old is None:  # première photo : impossible de savoir quel lien a servi
                return
            code = used_invite(old, new)

            existing = db.one(
                "SELECT * FROM partner_referrals WHERE guild_id=? AND member_id=?", (guild.id, member.id)
            )
            if existing:  # la personne revient : elle recompte pour son partenaire d'origine
                if existing["left_at"]:
                    db.run("UPDATE partner_referrals SET left_at=NULL WHERE id=?", (existing["id"],))
                    await self.refresh_partner(guild, existing["partner_id"], announce=True)
                return
            if code is None:
                return
            partner = db.one(
                "SELECT * FROM partners WHERE guild_id=? AND invite_code=? AND active=1", (guild.id, code)
            )
            if partner is None or partner["user_id"] == member.id:
                return
            min_days = int(db.get_config(guild.id, "partner_min_age_days", "0") or 0)
            if min_days and (datetime.datetime.now(datetime.timezone.utc) - member.created_at).days < min_days:
                return  # compte trop récent : ne compte pas (protection contre les faux comptes)
            db.run(
                "INSERT INTO partner_referrals (guild_id, partner_id, member_id, joined_at) VALUES (?, ?, ?, ?)",
                (guild.id, partner["id"], member.id, int(time.time())),
            )
            await self.refresh_partner(guild, partner["id"], announce=True)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        if member.bot:
            return
        row = db.one(
            "SELECT * FROM partner_referrals WHERE guild_id=? AND member_id=? AND left_at IS NULL",
            (member.guild.id, member.id),
        )
        if row is None:
            return
        db.run("UPDATE partner_referrals SET left_at=? WHERE id=?", (int(time.time()), row["id"]))
        await self.refresh_partner(member.guild, row["partner_id"], announce=False)

    # ---------- Paliers ----------
    async def refresh_partner(self, guild, partner_id, announce=False):
        partner = db.one("SELECT * FROM partners WHERE id=?", (partner_id,))
        if partner is None or not partner["active"]:
            return
        active = db.one(
            "SELECT COUNT(*) AS c FROM partner_referrals WHERE partner_id=? AND left_at IS NULL", (partner_id,)
        )["c"]
        tiers = db.rows("SELECT * FROM partner_tiers WHERE guild_id=? ORDER BY min_members", (guild.id,))
        current = tier_for(tiers, active)
        new_id = current["id"] if current else None
        old_id = partner["tier_id"]
        if new_id == old_id:
            return

        db.run("UPDATE partners SET tier_id=? WHERE id=?", (new_id, partner_id))
        member = guild.get_member(partner["user_id"])
        if member:
            await self.sync_roles(guild, member, current, tiers)

        # Un palier n'est annoncé qu'une seule fois, même si des membres partent puis reviennent
        best = partner["best_tier_min"] or 0
        if current and current["min_members"] > best:
            db.run("UPDATE partners SET best_tier_min=? WHERE id=?", (current["min_members"], partner_id))
            if announce:
                await self.announce(guild, partner, member, current, active)

    async def sync_roles(self, guild, member, current, tiers):
        tier_roles = {t["role_id"] for t in tiers if t["role_id"]}
        wanted = current["role_id"] if current and current["role_id"] else None
        try:
            to_remove = [r for r in member.roles if r.id in tier_roles and r.id != wanted]
            if to_remove:
                await member.remove_roles(*to_remove, reason="Palier partenaire")
            if wanted:
                role = guild.get_role(wanted)
                if role and role not in member.roles:
                    await member.add_roles(role, reason="Palier partenaire")
        except discord.HTTPException:
            logging.warning("Impossible de mettre à jour les rôles de palier de %s", member)

    async def announce(self, guild, partner, member, tier, active):
        embed = embeds.make("🏆 Nouveau palier partenaire", None, "success")
        embed.add_field(name="Partenaire", value=member.mention if member else partner["name"])
        embed.add_field(name="Palier", value=tier["name"])
        embed.add_field(name="Membres actifs", value=str(active))
        await send_log(guild, embed)

        if member:
            text = (
                f"Bravo ! Grâce à toi, **{active}** membres ont rejoint **{guild.name}**.\n"
                f"Tu passes au palier **{tier['name']}** 🎉"
            )
            if tier["reward"]:
                text += f"\n\n🎁 **Récompense :** {tier['reward']}"
            try:
                await member.send(embed=embeds.make("🏆 Nouveau palier !", text, "success"))
            except (discord.Forbidden, discord.HTTPException):
                pass

    # ---------- Appelé par le dashboard (dans la boucle du bot) ----------
    async def create_partner(self, guild_id, user_id, name, channel_id):
        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild else None
        if channel is None:
            raise ValueError("Salon introuvable.")
        invite = await channel.create_invite(
            max_age=0, max_uses=0, unique=True, reason=f"Lien partenaire : {name}"
        )
        db.run(
            "INSERT INTO partners (guild_id, user_id, name, invite_code, channel_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, name, invite.code, channel_id, int(time.time())),
        )
        await self.snapshot(guild)
        return invite.code

    async def remove_partner(self, guild_id, partner_id):
        guild = self.bot.get_guild(guild_id)
        partner = db.one("SELECT * FROM partners WHERE id=? AND guild_id=? AND active=1", (partner_id, guild_id))
        if guild is None or partner is None:
            return False
        db.run("UPDATE partners SET active=0, tier_id=NULL WHERE id=?", (partner_id,))
        try:
            for invite in await guild.invites():
                if invite.code == partner["invite_code"]:
                    await invite.delete(reason="Partenaire retiré")
                    break
        except (discord.Forbidden, discord.HTTPException):
            pass
        member = guild.get_member(partner["user_id"])
        if member:
            tiers = db.rows("SELECT * FROM partner_tiers WHERE guild_id=?", (guild_id,))
            await self.sync_roles(guild, member, None, tiers)
        await self.snapshot(guild)
        return True

    async def delete_tier(self, guild_id, tier_id):
        """Supprime un palier et retire son rôle aux partenaires qui l'avaient."""
        guild = self.bot.get_guild(guild_id)
        tier = db.one("SELECT * FROM partner_tiers WHERE id=? AND guild_id=?", (tier_id, guild_id))
        if tier is None:
            return False
        role = guild.get_role(tier["role_id"]) if guild and tier["role_id"] else None
        if role:
            for row in db.rows("SELECT user_id FROM partners WHERE guild_id=? AND active=1", (guild_id,)):
                member = guild.get_member(row["user_id"])
                if member and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Palier supprimé")
                    except discord.HTTPException:
                        pass
        db.run("DELETE FROM partner_tiers WHERE id=?", (tier_id,))
        await self.refresh_guild(guild_id)
        return True

    async def create_reward_ticket(self, guild_id, partner_id):
        """Ouvre un ticket « récompense » entre l'équipe et le partenaire. Retourne (id du salon, créé ?)."""
        guild = self.bot.get_guild(guild_id)
        partner = db.one("SELECT * FROM partners WHERE id=? AND guild_id=? AND active=1", (partner_id, guild_id))
        member = guild.get_member(partner["user_id"]) if guild and partner else None
        if member is None:
            raise ValueError("Partenaire introuvable.")
        active = db.one(
            "SELECT COUNT(*) AS c FROM partner_referrals WHERE partner_id=? AND left_at IS NULL", (partner_id,)
        )["c"]
        tier = tier_for(db.rows("SELECT * FROM partner_tiers WHERE guild_id=?", (guild_id,)), active)
        if tier is None:
            raise ValueError("Aucun palier atteint pour l'instant.")
        existing = find_ticket(guild, member.id)
        if existing:
            return existing.id, False
        channel = await create_system_ticket(
            guild, member, "recompense", "🎁 Récompense partenaire",
            [("🤝 Partenaire", partner["name"]), ("🏅 Palier", tier["name"]), ("👥 Membres actifs", active),
             ("🎁 Récompense", tier["reward"] or "À définir avec l'équipe")],
            intro="L'équipe va te remettre ta récompense.",
        )
        return channel.id, True

    async def refresh_guild(self, guild_id):
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        for row in db.rows("SELECT id FROM partners WHERE guild_id=? AND active=1", (guild_id,)):
            await self.refresh_partner(guild, row["id"], announce=False)


async def setup(bot):
    await bot.add_cog(Partners(bot))
