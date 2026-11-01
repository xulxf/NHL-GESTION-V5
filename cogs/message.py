import os
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

try:
    from utils import embeds
except ImportError:  # projet sans utils/embeds.py : on utilise des couleurs de secours
    embeds = None

FALLBACK_COLORS = {
    "info": 0x5865F2,
    "success": 0x57F287,
    "warn": 0xFEE75C,
    "error": 0xED4245,
    "mod": 0xEB459E,
}


def _mo(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} Mo"


async def _to_file(att: discord.Attachment, base: str) -> discord.File:
    """Télécharge la pièce jointe et la renomme (évite les noms de fichiers problématiques)."""
    ext = os.path.splitext(att.filename)[1].lower()
    return await att.to_file(filename=f"{base}{ext}")


def build_embed(titre, contenu, couleur, footer) -> discord.Embed:
    if embeds is not None:
        return embeds.make(titre, contenu, couleur, footer=footer)
    embed = discord.Embed(title=titre, description=contenu, color=FALLBACK_COLORS[couleur])
    if footer:
        embed.set_footer(text=footer)
    return embed


class Message(commands.Cog):
    """Envoyer un message personnalisé (embed) avec image et/ou vidéo."""

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="message", description="Envoyer un message personnalisé avec un embed")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.describe(
        channel="Salon où envoyer le message",
        titre="Titre de l'embed (facultatif)",
        contenu="Contenu du message (facultatif) — écris \\n pour un retour à la ligne",
        footer="Pied de page de l'embed (facultatif)",
        couleur="Couleur de l'embed",
        image="Image ou GIF à afficher (facultatif)",
        video="Vidéo à joindre (facultatif)",
    )
    async def message(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        titre: Optional[str] = None,
        contenu: Optional[str] = None,
        footer: Optional[str] = None,
        couleur: Optional[Literal["info", "success", "warn", "error", "mod"]] = None,
        image: Optional[discord.Attachment] = None,
        video: Optional[discord.Attachment] = None,
    ):
        if contenu:
            contenu = contenu.replace("\\n", "\n")  # "\n" tapé à la main = retour à la ligne

        if not (titre or contenu or image or video):
            return await interaction.response.send_message(
                "❌ Ton message est vide : ajoute un titre, un contenu, une image ou une vidéo.", ephemeral=True
            )
        if titre and len(titre) > 256:
            return await interaction.response.send_message("❌ Le titre est trop long (256 caractères max).", ephemeral=True)
        if contenu and len(contenu) > 4000:
            return await interaction.response.send_message("❌ Le contenu est trop long (4000 caractères max).", ephemeral=True)

        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.embed_links and perms.attach_files):
            return await interaction.response.send_message(
                f"❌ Il me manque des permissions dans {channel.mention} "
                "(Envoyer des messages, Intégrer des liens, Joindre des fichiers).",
                ephemeral=True,
            )

        limit = interaction.guild.filesize_limit
        for attachment, kind in ((image, "image"), (video, "video")):
            if attachment is None:
                continue
            if not (attachment.content_type or "").startswith(f"{kind}/"):
                return await interaction.response.send_message(
                    f"❌ Le fichier « {attachment.filename} » n'est pas une {kind} valide.", ephemeral=True
                )
            if attachment.size > limit:
                return await interaction.response.send_message(
                    f"❌ « {attachment.filename} » pèse {_mo(attachment.size)} : "
                    f"la limite de ce serveur est de {_mo(limit)}.",
                    ephemeral=True,
                )

        await interaction.response.defer(ephemeral=True)

        files = []
        embed = None
        try:
            if titre or contenu or image:
                embed = build_embed(titre, contenu, couleur or "info", footer)
            if image:
                image_file = await _to_file(image, "image")
                embed.set_image(url=f"attachment://{image_file.filename}")
                files.append(image_file)
            if video:
                files.append(await _to_file(video, "video"))
            sent = await channel.send(embed=embed, files=files)
        except discord.Forbidden:
            return await interaction.followup.send(
                "❌ Je n'ai pas la permission d'écrire ou d'envoyer des fichiers dans ce salon.", ephemeral=True
            )
        except discord.HTTPException:
            return await interaction.followup.send(
                "❌ Impossible d'envoyer ce message (fichier trop lourd ou invalide ?).", ephemeral=True
            )

        await interaction.followup.send(f"✅ Message envoyé dans {channel.mention} : {sent.jump_url}", ephemeral=True)


async def setup(bot):
    # Remplace une ancienne commande /message déjà enregistrée par un autre fichier
    # (ce cog doit donc être chargé APRÈS les autres)
    bot.tree.remove_command("message")
    await bot.add_cog(Message(bot))
