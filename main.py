import logging
import os

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from utils import db, embeds

load_dotenv()
logging.basicConfig(level=logging.INFO)

EXTENSIONS = (
    "config", "events", "moderation", "automod", "tickets", "utility", "message",
    "orders", "reviews", "faq", "community", "giveaways", "levels", "antiraid", "partners", "counters",
    "tools", "ticket_tools", "extras", "fivem",
)

intents = discord.Intents.default()
intents.members = True          # intent privilégié : à activer dans le portail
intents.message_content = True  # intent privilégié : à activer dans le portail


class ServerBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self):
        db.init()
        for ext in EXTENSIONS:
            await self.load_extension(f"cogs.{ext}")

        dev_guild = os.getenv("DEV_GUILD_ID")
        if dev_guild:  # synchronisation instantanée sur ton serveur
            guild = discord.Object(id=int(dev_guild))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:  # synchronisation globale (jusqu'à ~1h de délai)
            await self.tree.sync()

    async def on_ready(self):
        await self.change_presence(
            status=discord.Status.dnd,
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="lux.fx"
            ),
        )
        logging.info("Connecté en tant que %s (%s)", self.user, self.user.id)


bot = ServerBot()


@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)

    if isinstance(error, app_commands.MissingPermissions):
        text = "Tu n'as pas la permission d'utiliser cette commande."
    elif isinstance(error, app_commands.BotMissingPermissions):
        missing = ", ".join(error.missing_permissions)
        text = f"Il me manque des permissions : `{missing}`."
    elif isinstance(original, discord.Forbidden):
        text = "Je ne peux pas faire ça. Vérifie que mon rôle est bien **au-dessus** du membre ciblé."
    else:
        text = "Une erreur inattendue est survenue."
        logging.exception("Erreur de commande", exc_info=error)

    embed = embeds.error(text)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


if __name__ == "__main__":
    bot.run(os.getenv("DISCORD_TOKEN"))
