import re
import time
from collections import defaultdict, deque
from datetime import timedelta

import discord
from discord.ext import commands

from utils import db, embeds
from utils.logs import send_log

INVITE_RE = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite)/[\w-]+", re.I)

SPAM_LIMIT = 6          # messages...
SPAM_WINDOW = 5         # ...en 5 secondes
SPAM_TIMEOUT_MIN = 5    # => sourdine de 5 minutes


class AutoMod(commands.Cog):
    """Anti-spam et blocage des invitations externes."""

    def __init__(self, bot):
        self.bot = bot
        self.history = defaultdict(lambda: deque(maxlen=SPAM_LIMIT))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        member = message.author
        if not isinstance(member, discord.Member) or member.guild_permissions.manage_messages:
            return

        guild_id = message.guild.id

        # Anti-invitations
        if db.get_config(guild_id, "automod_invites", "1") == "1" and INVITE_RE.search(message.content):
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            await message.channel.send(
                embed=embeds.error(f"{member.mention}, les invitations vers d'autres serveurs sont interdites ici.", "Automod"),
                delete_after=8,
            )
            log = embeds.make("🛡️ Invitation bloquée", None, "warn")
            log.add_field(name="Auteur", value=member.mention)
            log.add_field(name="Salon", value=message.channel.mention)
            log.add_field(name="Message", value=embeds.cut(message.content), inline=False)
            await send_log(message.guild, log)
            return

        # Anti-spam
        if db.get_config(guild_id, "automod_spam", "1") == "1":
            now = time.monotonic()
            queue = self.history[(guild_id, member.id)]
            queue.append(now)
            if len(queue) == SPAM_LIMIT and now - queue[0] <= SPAM_WINDOW:
                queue.clear()
                try:
                    await member.timeout(timedelta(minutes=SPAM_TIMEOUT_MIN), reason="Anti-spam")
                except discord.HTTPException:
                    return
                await message.channel.send(
                    embed=embeds.make(
                        "🛑 Anti-spam",
                        f"{member.mention} a été mis en sourdine {SPAM_TIMEOUT_MIN} minutes pour spam.",
                        "warn",
                    ),
                    delete_after=10,
                )
                log = embeds.make("🛑 Anti-spam déclenché", None, "warn")
                log.add_field(name="Membre", value=member.mention)
                log.add_field(name="Salon", value=message.channel.mention)
                await send_log(message.guild, log)


async def setup(bot):
    await bot.add_cog(AutoMod(bot))
