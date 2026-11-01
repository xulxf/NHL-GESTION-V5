import time
from collections import deque
from datetime import timedelta

import discord
from discord.ext import commands

from utils import db, embeds
from utils.logs import send_log

RAID_JOINS = 8         # nombre d'arrivées...
RAID_WINDOW = 20       # ...en moins de 20 secondes = raid probable
RAID_DURATION = 600    # le mode raid dure 10 minutes
TIMEOUT_MINUTES = 30


class AntiRaid(commands.Cog):
    """Détecte les arrivées massives de comptes (raids)."""

    def __init__(self, bot):
        self.bot = bot
        self.joins = {}        # guild_id -> deque[(timestamp, membre)]
        self.raid_until = {}   # guild_id -> timestamp de fin du mode raid

    @staticmethod
    async def punish(member: discord.Member, mode: str):
        try:
            if mode == "timeout":
                await member.timeout(timedelta(minutes=TIMEOUT_MINUTES), reason="Anti-raid")
            elif mode == "kick":
                await member.kick(reason="Anti-raid")
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        mode = db.get_config(guild.id, "antiraid", "alerte")
        if mode == "off" or member.bot:
            return

        now = time.monotonic()
        queue = self.joins.setdefault(guild.id, deque())
        queue.append((now, member))
        while queue and now - queue[0][0] > RAID_WINDOW:
            queue.popleft()

        if self.raid_until.get(guild.id, 0) > now:  # raid déjà en cours
            await self.punish(member, mode)
            return

        if len(queue) >= RAID_JOINS:
            self.raid_until[guild.id] = now + RAID_DURATION
            recent = [m for _, m in queue]
            actions = {
                "alerte": "alerte uniquement",
                "timeout": f"mise en sourdine de {TIMEOUT_MINUTES} min des nouveaux arrivants",
                "kick": "expulsion des nouveaux arrivants",
            }
            embed = embeds.make(
                "🚨 Raid probable détecté",
                f"**{len(recent)}** comptes ont rejoint en moins de {RAID_WINDOW} secondes.\n"
                f"Action : **{actions.get(mode, mode)}** pendant {RAID_DURATION // 60} minutes.",
                "error",
            )
            embed.add_field(name="Comptes", value=", ".join(m.mention for m in recent)[:1000])
            staff_id = db.get_int(guild.id, "staff_role")
            await send_log(guild, embed, content=f"<@&{staff_id}>" if staff_id else None)
            for m in recent:
                await self.punish(m, mode)


async def setup(bot):
    await bot.add_cog(AntiRaid(bot))
