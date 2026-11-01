import asyncio
import io
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.faq import send_faq
from cogs.reviews import send_review_request
from utils import branding, db, embeds, status
from utils.logs import send_log

REMIND_AFTER = 3 * 86400   # relance du client après 3 jours sans message
CLOSE_AFTER = 7 * 86400    # fermeture automatique après 7 jours sans message

# Champ "Détails" : toujours affiché en dernier dans le formulaire
DETAILS_FIELD = {
    "id": "details", "label": "Détails", "emoji": "📝",
    "placeholder": "Donne-nous un maximum d'informations pour t'aider vite",
    "long": True, "max": 1000,
}

# Catégories proposées aux clients — modifie-les librement.
# "fields" = les champs du formulaire (4 maximum), affichés avant "Détails".
# Chaque champ : id, label (nom affiché), emoji, placeholder (texte d'aide).
# "is_order": True = catégorie soumise à l'ouverture/fermeture des commandes (/commandes).
CATEGORIES = {
    "commande": {
        "label": "Commande", "emoji": "🛒",
        "style": discord.ButtonStyle.success,
        "desc": "Passer ou suivre une commande",
        "is_order": True,
        "fields": [
            {"id": "arme", "label": "Arme", "emoji": "⚔️", "placeholder": "Quelle arme ?"},
            {"id": "couleur", "label": "Couleur", "emoji": "🎨", "placeholder": "Quelle couleur ?"},
            {"id": "effet", "label": "Effet", "emoji": "✨", "placeholder": "Quel effet ?"},
            {"id": "nom", "label": "Nom", "emoji": "🏷️", "placeholder": "Quel nom ?"},
        ],
    },
    "support": {
        "label": "Support", "emoji": "🛠️",
        "style": discord.ButtonStyle.primary,
        "desc": "Un problème ou un bug à résoudre",
    },
    "question": {
        "label": "Question", "emoji": "💬",
        "style": discord.ButtonStyle.secondary,
        "desc": "Une question générale",
    },
    "partenariat": {
        "label": "Partenariat", "emoji": "🤝",
        "style": discord.ButtonStyle.secondary,
        "desc": "Proposer une collaboration",
    },
}


# ---------- Utilitaires ----------
def ticket_owner_id(channel) -> Optional[int]:
    topic = getattr(channel, "topic", None) or ""
    if topic.startswith("ticket:"):
        try:
            return int(topic.split(":")[1])
        except (ValueError, IndexError):
            return None
    return None


def find_ticket(guild: discord.Guild, user_id: int):
    return next((c for c in guild.text_channels if ticket_owner_id(c) == user_id), None)


def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.manage_channels:
        return True
    staff_id = db.get_int(member.guild.id, "staff_role")
    return bool(staff_id) and any(r.id == staff_id for r in member.roles)


def set_field(embed: discord.Embed, key: str, value: str) -> bool:
    """Modifie la valeur du champ d'un embed dont le nom contient `key`."""
    for i, field in enumerate(embed.fields):
        if key in field.name:
            embed.set_field_at(i, name=field.name, value=value, inline=field.inline)
            return True
    return False


def orders_closed_embed(guild_id: int) -> discord.Embed:
    text = db.get_config(guild_id, "orders_closed_msg") or "Les commandes sont temporairement fermées. Reviens bientôt !"
    return embeds.make("⛔ Commandes fermées", text, "warn")


def pick_assignee(guild: discord.Guild):
    """Prochain membre de l'équipe, à tour de rôle (si l'assignation automatique est activée)."""
    if db.get_config(guild.id, "auto_assign", "0") != "1":
        return None
    role = guild.get_role(db.get_int(guild.id, "staff_role") or 0)
    candidates = sorted((m for m in (role.members if role else []) if not m.bot), key=lambda m: m.id)
    if not candidates:
        return None
    last = db.get_int(guild.id, "assign_cursor") or 0
    chosen = next((m for m in candidates if m.id > last), candidates[0])
    db.set_config(guild.id, "assign_cursor", chosen.id)
    return chosen


async def create_system_ticket(guild, user, key, title, fields, intro="L'équipe va s'occuper de ta demande."):
    """Crée un ticket sans formulaire (livraison d'achat, récompense partenaire…)."""
    number = (db.get_int(guild.id, "ticket_counter") or 0) + 1
    db.set_config(guild.id, "ticket_counter", number)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            embed_links=True, attach_files=True, manage_channels=True,
        ),
    }
    staff = guild.get_role(db.get_int(guild.id, "staff_role") or 0)
    if staff:
        overwrites[staff] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True
        )
    parent = guild.get_channel(db.get_int(guild.id, "ticket_category") or 0)
    if not isinstance(parent, discord.CategoryChannel):
        parent = None
    channel = await guild.create_text_channel(
        name=f"{key}-{number:04d}", category=parent, overwrites=overwrites,
        topic=f"ticket:{user.id}:{key}", reason=f"Ticket automatique ({key})",
    )
    assignee = pick_assignee(guild)
    embed = embeds.make(f"{title} — #{number:04d}", f"Bonjour {user.mention} ! {intro}", "info", section="ticket")
    for name, value in fields:
        embed.add_field(name=name, value=str(value)[:1000], inline=True)
    embed.add_field(name="👤 Client", value=user.mention)
    embed.add_field(name="📦 Statut", value=status.label("attente"))
    embed.add_field(name="🧑‍💼 Responsable", value=assignee.mention if assignee else "*Pas encore assigné*")
    embed.set_thumbnail(url=user.display_avatar.url)
    mentions = f"{user.mention} {assignee.mention if assignee else (staff.mention if staff else '')}".strip()
    message = await channel.send(content=mentions, embed=embed, view=TicketControlView(claimed=bool(assignee)))
    db.run(
        "INSERT INTO tickets (guild_id, number, channel_id, user_id, category, status, message_id, created_at, claimed_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (guild.id, number, channel.id, user.id, key, "attente", message.id, int(time.time()), assignee.id if assignee else None),
    )
    return channel


async def build_transcript(channel: discord.TextChannel) -> str:
    lines = [f"Transcription de #{channel.name} — {channel.guild.name}", ""]
    async for m in channel.history(limit=1000, oldest_first=True):
        stamp = m.created_at.strftime("%d/%m/%Y %H:%M")
        content = m.clean_content
        for e in m.embeds:
            if e.title:
                content += f" [embed : {e.title}]"
        for a in m.attachments:
            content += f"\n[Fichier] {a.url}"
        lines.append(f"[{stamp}] {m.author.display_name} : {content}")
    return "\n".join(lines)


async def finalize_ticket(guild: discord.Guild, channel: discord.TextChannel, closer: str, reason: str, delay: int = 5, closer_name: str = None):
    """Transcription, logs, message privé, demande d'avis puis suppression du salon."""
    owner_id = ticket_owner_id(channel)
    row = db.one("SELECT * FROM tickets WHERE channel_id=?", (channel.id,))

    transcript = await build_transcript(channel)
    db.run(
        "INSERT INTO transcripts (guild_id, ticket_number, channel_name, user_id, closed_by, content, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (guild.id, row["number"] if row else 0, channel.name, owner_id, closer_name or closer,
         transcript[:200000], int(time.time())),
    )

    def new_file():
        return discord.File(
            io.BytesIO(transcript.encode("utf-8")),
            filename=f"transcription-{channel.name}.txt",
        )

    log = embeds.make("🔒 Ticket fermé", None, "warn")
    log.add_field(name="Ticket", value=f"#{channel.name}")
    log.add_field(name="Client", value=f"<@{owner_id}>" if owner_id else "?")
    log.add_field(name="Fermé par", value=closer)
    log.add_field(name="Raison", value=reason, inline=False)
    await send_log(guild, log, file=new_file())

    owner = guild.get_member(owner_id) if owner_id else None
    if owner:
        dm = embeds.make(
            "🔒 Ton ticket a été fermé",
            f"**Serveur :** {guild.name}\n**Ticket :** {channel.name}\n**Raison :** {reason}\n\n"
            "Tu trouveras la transcription de la conversation en pièce jointe. Merci de ta confiance !",
            "info",
        )
        try:
            await owner.send(embed=dm, file=new_file())
        except (discord.Forbidden, discord.HTTPException):
            pass

    if row:
        db.run("UPDATE tickets SET closed_at=? WHERE id=?", (int(time.time()), row["id"]))
        if owner:
            await send_review_request(guild, owner, row["number"], row["category"])

    if delay:
        await asyncio.sleep(delay)
    await channel.delete(reason=f"Ticket fermé ({closer}) : {reason}")


async def close_ticket(interaction: discord.Interaction, reason: str):
    await interaction.response.send_message(
        embed=embeds.make(
            "🔒 Fermeture du ticket",
            "Enregistrement de la transcription… ce salon sera supprimé dans quelques secondes.",
            "warn",
        )
    )
    await finalize_ticket(
        interaction.guild, interaction.channel, interaction.user.mention, reason,
        closer_name=interaction.user.display_name,
    )


# ---------- Modals ----------
class TicketModal(discord.ui.Modal):
    """Formulaire construit à partir des champs de la catégorie choisie."""

    def __init__(self, key: str):
        cat = CATEGORIES[key]
        super().__init__(title=f"{cat['emoji']} {cat['label']}"[:45])
        self.key = key
        self.inputs = []  # liste de (définition du champ, TextInput)
        for field in cat.get("fields", [])[:4] + [DETAILS_FIELD]:
            style = discord.TextStyle.paragraph if field.get("long") else discord.TextStyle.short
            text_input = discord.ui.TextInput(
                label=field["label"][:45],
                placeholder=field.get("placeholder"),
                style=style,
                max_length=field.get("max", 100),
                required=field.get("required", True),
            )
            self.add_item(text_input)
            self.inputs.append((field, text_input))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild, user, cat = interaction.guild, interaction.user, CATEGORIES[self.key]

        if cat.get("is_order") and db.get_config(guild.id, "orders_open", "1") != "1":
            return await interaction.followup.send(embed=orders_closed_embed(guild.id), ephemeral=True)

        existing = find_ticket(guild, user.id)
        if existing:
            return await interaction.followup.send(
                embed=embeds.error(f"Tu as déjà un ticket ouvert : {existing.mention}"), ephemeral=True
            )

        number = (db.get_int(guild.id, "ticket_counter") or 0) + 1
        db.set_config(guild.id, "ticket_counter", number)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, attach_files=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                embed_links=True, attach_files=True, manage_channels=True,
            ),
        }
        staff_id = db.get_int(guild.id, "staff_role")
        staff = guild.get_role(staff_id) if staff_id else None
        if staff:
            overwrites[staff] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, attach_files=True
            )

        parent_id = db.get_int(guild.id, "ticket_category")
        parent = guild.get_channel(parent_id) if parent_id else None
        if not isinstance(parent, discord.CategoryChannel):
            parent = interaction.channel.category

        channel = await guild.create_text_channel(
            name=f"{self.key}-{number:04d}",
            category=parent,
            overwrites=overwrites,
            topic=f"ticket:{user.id}:{self.key}",
            reason=f"Ticket de {user}",
        )

        embed = embeds.make(
            f"{cat['emoji']} Ticket #{number:04d} — {cat['label']}",
            f"Bonjour {user.mention}, merci de nous avoir contactés !\n"
            "Un membre de l'équipe va te répondre dès que possible.",
            "info",
            footer="Utilise les boutons ci-dessous pour gérer le ticket",
            section="ticket",
        )
        # Champs du formulaire, puis "Détails" en bas
        for field, text_input in self.inputs:
            value = text_input.value.strip()
            if not value:
                continue
            embed.add_field(
                name=f"{field['emoji']} {field['label']}",
                value=value,
                inline=field["id"] != "details",
            )
        embed.add_field(name="👤 Client", value=user.mention)
        embed.add_field(name="🏷️ Catégorie", value=cat["label"])
        assignee = pick_assignee(guild)
        embed.add_field(name="📦 Statut", value=status.label("attente"))
        embed.add_field(name="🧑‍💼 Responsable", value=assignee.mention if assignee else "*Pas encore assigné*")
        embed.set_thumbnail(url=user.display_avatar.url)

        who = assignee.mention if assignee else (staff.mention if staff else "")
        mentions = f"{user.mention} {who}".strip()
        message = await channel.send(content=mentions, embed=embed, view=TicketControlView(claimed=bool(assignee)))

        db.run(
            "INSERT INTO tickets (guild_id, number, channel_id, user_id, category, status, message_id, created_at, claimed_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild.id, number, channel.id, user.id, self.key, "attente", message.id, int(time.time()),
             assignee.id if assignee else None),
        )

        await interaction.followup.send(
            embed=embeds.success(f"Ton ticket est ouvert : {channel.mention}"), ephemeral=True
        )

        log = embeds.make("🎫 Ticket ouvert", None, "success")
        log.add_field(name="Salon", value=channel.mention)
        log.add_field(name="Client", value=user.mention)
        log.add_field(name="Catégorie", value=cat["label"])
        await send_log(guild, log)


class CloseModal(discord.ui.Modal, title="Fermer le ticket"):
    reason = discord.ui.TextInput(
        label="Raison de la fermeture",
        placeholder="Facultatif",
        required=False,
        max_length=200,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await close_ticket(interaction, self.reason.value or "Aucune raison fournie")


# ---------- Vues ----------
async def start_ticket(interaction: discord.Interaction, key: str):
    """Vérifie que le client peut ouvrir un ticket, puis affiche le formulaire de la catégorie."""
    if CATEGORIES[key].get("is_order") and db.get_config(interaction.guild_id, "orders_open", "1") != "1":
        return await interaction.response.send_message(
            embed=orders_closed_embed(interaction.guild_id), ephemeral=True
        )
    existing = find_ticket(interaction.guild, interaction.user.id)
    if existing:
        return await interaction.response.send_message(
            embed=embeds.error(f"Tu as déjà un ticket ouvert : {existing.mention}"), ephemeral=True
        )
    await interaction.response.send_modal(TicketModal(key))


class TicketSelect(discord.ui.Select):
    """Liste déroulante : une entrée par catégorie + la FAQ."""

    def __init__(self):
        options = [
            discord.SelectOption(label=cat["label"], value=key, emoji=cat["emoji"], description=cat["desc"][:100])
            for key, cat in CATEGORIES.items()
        ]
        options.append(
            discord.SelectOption(label="FAQ", value="faq", emoji="❓", description="Les réponses aux questions fréquentes")
        )
        super().__init__(
            placeholder="📩 Choisis le type de demande…",
            min_values=1, max_values=1, options=options, custom_id="ticket:select",
        )

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        message = interaction.message
        if choice == "faq":
            await send_faq(interaction)
        elif choice in CATEGORIES:
            await start_ticket(interaction, choice)
        else:  # catégorie supprimée depuis la publication du panneau
            await interaction.response.send_message(
                embed=embeds.error("Cette option n'existe plus : demande à l'équipe de republier le panneau."),
                ephemeral=True,
            )
        try:  # remet la liste à zéro pour que la même entrée puisse être rechoisie
            await message.edit(view=SelectPanelView())
        except discord.HTTPException:
            pass


class SelectPanelView(discord.ui.View):
    """Panneau avec une liste déroulante (persistant)."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect())


class PanelView(discord.ui.View):
    """Panneau avec un bouton par catégorie + FAQ (persistant, ancien affichage)."""

    def __init__(self):
        super().__init__(timeout=None)
        for key, cat in CATEGORIES.items():
            button = discord.ui.Button(
                label=cat["label"],
                emoji=cat["emoji"],
                style=cat["style"],
                custom_id=f"ticket:open:{key}",
            )
            button.callback = self._make_callback(key)
            self.add_item(button)

        faq_button = discord.ui.Button(
            label="FAQ", emoji="❓", style=discord.ButtonStyle.secondary, custom_id="ticket:faq"
        )
        faq_button.callback = self._faq_callback
        self.add_item(faq_button)

    @staticmethod
    async def _faq_callback(interaction: discord.Interaction):
        await send_faq(interaction)

    @staticmethod
    def _make_callback(key: str):
        async def callback(interaction: discord.Interaction):
            await start_ticket(interaction, key)

        return callback


def panel_view(style: str):
    """Vue du panneau : « liste » (par défaut) ou « boutons »."""
    return PanelView() if style == "boutons" else SelectPanelView()


class TicketControlView(discord.ui.View):
    """Boutons dans le ticket : prise en charge et fermeture (persistants)."""

    def __init__(self, claimed: bool = False):
        super().__init__(timeout=None)
        if claimed:
            self.claim.disabled = True

    @discord.ui.button(label="Prendre en charge", emoji="🙋", style=discord.ButtonStyle.success, custom_id="ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                embed=embeds.error("Seule l'équipe peut prendre un ticket en charge."), ephemeral=True
            )
        embed = interaction.message.embeds[0]
        set_field(embed, "Responsable", interaction.user.mention)
        db.run(
            "UPDATE tickets SET claimed_by=? WHERE channel_id=? AND claimed_by IS NULL",
            (interaction.user.id, interaction.channel_id),
        )
        await interaction.response.edit_message(embed=embed, view=TicketControlView(claimed=True))
        await interaction.channel.send(
            embed=embeds.success(f"{interaction.user.mention} s'occupe de ta demande.", "Ticket pris en charge")
        )

    @discord.ui.button(label="Fermer", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        owner_id = ticket_owner_id(interaction.channel)
        if owner_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Ce salon n'est pas un ticket."), ephemeral=True
            )
        if interaction.user.id != owner_id and not is_staff(interaction.user):
            return await interaction.response.send_message(
                embed=embeds.error("Seul l'auteur du ticket ou l'équipe peut le fermer."), ephemeral=True
            )
        await interaction.response.send_modal(CloseModal())


# ---------- Cog ----------
class Tickets(commands.Cog):
    """Système de tickets clients."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # Vues persistantes : les boutons restent actifs après un redémarrage
        self.bot.add_view(PanelView())
        self.bot.add_view(SelectPanelView())
        self.bot.add_view(TicketControlView())
        self.inactivity_check.start()

    async def cog_unload(self):
        self.inactivity_check.cancel()

    # ----- Délai de première réponse (statistiques) -----
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or ticket_owner_id(message.channel) is None:
            return
        row = db.one(
            "SELECT id, user_id, first_response_at FROM tickets WHERE channel_id=? AND closed_at IS NULL",
            (message.channel.id,),
        )
        if row is None or row["first_response_at"] or message.author.id == row["user_id"]:
            return
        if isinstance(message.author, discord.Member) and is_staff(message.author):
            db.run("UPDATE tickets SET first_response_at=? WHERE id=?", (int(time.time()), row["id"]))

    # ----- Relance et fermeture automatiques -----
    @tasks.loop(minutes=30)
    async def inactivity_check(self):
        now = time.time()
        for row in db.rows("SELECT * FROM tickets WHERE closed_at IS NULL"):
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None:
                continue
            channel = guild.get_channel(row["channel_id"])
            if channel is None:  # salon supprimé à la main
                db.run("UPDATE tickets SET closed_at=? WHERE id=?", (int(now), row["id"]))
                continue

            hours = int(db.get_config(guild.id, "staff_reminder_hours", "4") or 0)
            if hours and not row["first_response_at"] and not row["staff_reminded"] and now - row["created_at"] >= hours * 3600:
                db.run("UPDATE tickets SET staff_reminded=1 WHERE id=?", (row["id"],))
                staff_id = db.get_int(guild.id, "staff_role")
                who = f"<@{row['claimed_by']}>" if row["claimed_by"] else (f"<@&{staff_id}>" if staff_id else None)
                try:
                    await channel.send(
                        content=who,
                        embed=embeds.make("⏰ Ticket sans réponse", f"Ce ticket attend une réponse de l'équipe depuis plus de {hours} h.", "warn"),
                    )
                except discord.HTTPException:
                    pass

            last = row["created_at"]
            try:
                async for m in channel.history(limit=50):
                    if not m.author.bot:
                        last = m.created_at.timestamp()
                        break
                idle = now - last

                if idle >= CLOSE_AFTER:
                    await finalize_ticket(
                        guild, channel, "Fermeture automatique",
                        f"Ticket inactif depuis plus de {CLOSE_AFTER // 86400} jours", delay=0,
                    )
                elif idle >= REMIND_AFTER and not row["reminded"]:
                    db.run("UPDATE tickets SET reminded=1 WHERE id=?", (row["id"],))
                    days_left = (CLOSE_AFTER - REMIND_AFTER) // 86400
                    await channel.send(
                        content=f"<@{row['user_id']}>",
                        embed=embeds.make(
                            "⏰ Toujours là ?",
                            f"Nous n'avons plus de nouvelles depuis {REMIND_AFTER // 86400} jours.\n"
                            "Si ta demande est réglée, tu peux fermer le ticket ; sinon réponds ici.\n"
                            f"Sans réponse, il sera fermé automatiquement dans {days_left} jours.",
                            "warn",
                        ),
                    )
            except discord.HTTPException:
                continue

    @inactivity_check.before_loop
    async def before_inactivity_check(self):
        await self.bot.wait_until_ready()

    # ----- Commandes -----
    @app_commands.command(name="ticket-panel", description="Poster le panneau d'ouverture de tickets")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @app_commands.describe(
        titre="Titre du panneau (facultatif)",
        description="Texte d'introduction (facultatif)",
        affichage="Liste déroulante (par défaut) ou boutons",
    )
    @app_commands.choices(affichage=[
        app_commands.Choice(name="Liste déroulante", value="liste"),
        app_commands.Choice(name="Boutons", value="boutons"),
    ])
    async def panel(
        self,
        interaction: discord.Interaction,
        titre: Optional[str] = None,
        description: Optional[str] = None,
        affichage: Optional[app_commands.Choice[str]] = None,
    ):
        intro = description or (
            "Une question, une commande, un souci ? Notre équipe est là pour t'aider.\n"
            "Choisis la catégorie qui correspond à ta demande pour ouvrir un ticket privé."
        )
        lines = [f"{c['emoji']} **{c['label']}** — {c['desc']}" for c in CATEGORIES.values()]
        lines.append("❓ **FAQ** — Les réponses aux questions fréquentes")
        embed = embeds.make(
            titre or "🎫 Support & Commandes",
            f"{intro}\n\n" + "\n".join(lines),
            "info",
            footer="Un seul ticket ouvert à la fois • Ta demande reste privée",
            section="ticket",
        )
        embed.timestamp = None
        guild_icon = interaction.guild.icon.url if interaction.guild.icon else None
        thumb = branding.logo("ticket") or guild_icon
        if thumb:
            embed.set_thumbnail(url=thumb)
        banner = branding.image("ticket")
        if banner:
            embed.set_image(url=banner)
        await interaction.channel.send(embed=embed, view=panel_view(affichage.value if affichage else "liste"))
        await interaction.response.send_message(embed=embeds.success("Panneau envoyé."), ephemeral=True)

    @app_commands.command(name="ticket-add", description="Ajouter un membre au ticket")
    @app_commands.guild_only()
    async def ticket_add(self, interaction: discord.Interaction, membre: discord.Member):
        if ticket_owner_id(interaction.channel) is None:
            return await interaction.response.send_message(embed=embeds.error("Ce salon n'est pas un ticket."), ephemeral=True)
        if not is_staff(interaction.user):
            return await interaction.response.send_message(embed=embeds.error("Réservé à l'équipe."), ephemeral=True)
        await interaction.channel.set_permissions(
            membre, view_channel=True, send_messages=True, read_message_history=True, attach_files=True
        )
        await interaction.response.send_message(embed=embeds.success(f"{membre.mention} a été ajouté au ticket."))

    @app_commands.command(name="ticket-remove", description="Retirer un membre du ticket")
    @app_commands.guild_only()
    async def ticket_remove(self, interaction: discord.Interaction, membre: discord.Member):
        owner_id = ticket_owner_id(interaction.channel)
        if owner_id is None:
            return await interaction.response.send_message(embed=embeds.error("Ce salon n'est pas un ticket."), ephemeral=True)
        if not is_staff(interaction.user):
            return await interaction.response.send_message(embed=embeds.error("Réservé à l'équipe."), ephemeral=True)
        if membre.id == owner_id:
            return await interaction.response.send_message(embed=embeds.error("Tu ne peux pas retirer l'auteur du ticket."), ephemeral=True)
        await interaction.channel.set_permissions(membre, overwrite=None)
        await interaction.response.send_message(embed=embeds.success(f"{membre.mention} a été retiré du ticket."))


async def setup(bot):
    await bot.add_cog(Tickets(bot))
