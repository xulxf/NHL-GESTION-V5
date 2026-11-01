"""Dashboard web : connexion Discord (OAuth2) puis réglages du bot pour chaque serveur.

Le dashboard tourne dans le même programme que le bot (voir start.py) : il partage donc
la même base de données et lit directement les salons / rôles connus du bot.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from functools import wraps

import discord
from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from utils import audit, db, tebex
from utils import status as order_status
from utils.helpers import level_progress, tier_progress

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (dashboard, 1.0)"

# ---------- Définition des pages de réglages ----------
# type : channel | category | role | toggle | choice | text
# assignable : le bot donne ce rôle lui-même (il doit être sous le rôle du bot)
PAGES = {
    "general": {
        "title": "Général", "icon": "⚙️",
        "desc": "Les salons et rôles de base du serveur.",
        "fields": [
            {"key": "log_channel", "label": "Salon des logs", "type": "channel", "core": True,
             "help": "Arrivées, départs, sanctions, tickets, alertes…"},
            {"key": "welcome_channel", "label": "Salon de bienvenue", "type": "channel", "core": True,
             "help": "Message affiché quand quelqu'un rejoint le serveur."},
            {"key": "welcome_title", "label": "Titre du message de bienvenue", "type": "text", "max": 100,
             "help": "Facultatif. Variables : {nom}, {membre}, {serveur}, {numero}."},
            {"key": "welcome_text", "label": "Texte du message de bienvenue", "type": "text", "max": 500,
             "help": "Facultatif. Mêmes variables : {membre} mentionne la personne, {numero} = nombre de membres."},
            {"key": "auto_role", "label": "Rôle automatique", "type": "role", "assignable": True,
             "help": "Donné à chaque nouveau membre."},
            {"key": "staff_role", "label": "Rôle de l'équipe", "type": "role", "core": True,
             "help": "Accès aux tickets, aux commandes d'équipe et aux alertes anti-raid."},
        ],
    },
    "tickets": {
        "title": "Tickets & commandes", "icon": "🎫",
        "desc": "Tickets clients, avis, vitrine et ouverture des commandes.",
        "fields": [
            {"key": "ticket_category", "label": "Catégorie des tickets", "type": "category", "core": True,
             "help": "Les salons de tickets seront créés dans cette catégorie."},
            {"key": "review_channel", "label": "Salon des avis clients", "type": "channel", "core": True,
             "help": "Les avis laissés après un ticket y sont publiés."},
            {"key": "showcase_channel", "label": "Salon de la vitrine", "type": "channel",
             "help": "Destination de la commande /realisation."},
            {"key": "client_role", "label": "Rôle client", "type": "role", "assignable": True,
             "help": "Donné automatiquement quand une commande passe à « Livrée »."},
            {"key": "auto_assign", "label": "Assignation automatique des tickets", "type": "toggle", "default": "0",
             "help": "Chaque nouveau ticket est confié à tour de rôle à un membre de l'équipe."},
            {"key": "staff_reminder_hours", "label": "Rappel à l'équipe sans réponse", "type": "choice", "default": "4",
             "options": [("0", "Désactivé"), ("2", "Après 2 heures"), ("4", "Après 4 heures"), ("8", "Après 8 heures"), ("24", "Après 24 heures")],
             "help": "Le bot ping l'équipe si personne n'a répondu au ticket."},
            {"key": "orders_open", "label": "Commandes ouvertes", "type": "toggle", "default": "1",
             "help": "Désactivé : le bouton « Commande » affiche le message ci-dessous."},
            {"key": "orders_closed_msg", "label": "Message quand les commandes sont fermées", "type": "text", "max": 500,
             "help": "Facultatif."},
        ],
    },
    "moderation": {
        "title": "Modération", "icon": "🛡️",
        "desc": "Automodération et protection contre les raids.",
        "fields": [
            {"key": "automod_spam", "label": "Anti-spam", "type": "toggle", "default": "1",
             "help": "Sourdine automatique de 5 minutes en cas de messages en rafale."},
            {"key": "automod_invites", "label": "Blocage des invitations Discord", "type": "toggle", "default": "1",
             "help": "Supprime les liens d'invitation vers d'autres serveurs."},
            {"key": "banned_words", "label": "Mots interdits", "type": "text", "max": 1500,
             "help": "Sépare les mots par des virgules ou des retours à la ligne. Les messages concernés sont supprimés (l'équipe est exemptée)."},
            {"key": "antiraid", "label": "Anti-raid", "type": "choice", "default": "alerte",
             "options": [
                 ("off", "Désactivé"),
                 ("alerte", "Alerte uniquement"),
                 ("timeout", "Alerte + sourdine des nouveaux arrivants"),
                 ("kick", "Alerte + expulsion des nouveaux arrivants"),
             ],
             "help": "Que faire quand beaucoup de comptes arrivent en quelques secondes."},
        ],
    },
    "community": {
        "title": "Communauté", "icon": "🎉",
        "desc": "Suggestions, règlement et niveaux.",
        "fields": [
            {"key": "suggestion_channel", "label": "Salon des suggestions", "type": "channel",
             "help": "Destination de la commande /suggestion."},
            {"key": "rules_role", "label": "Rôle du règlement", "type": "role", "assignable": True,
             "help": "Donné quand un membre clique sur « J'accepte le règlement »."},
            {"key": "levels_enabled", "label": "Système de niveaux", "type": "toggle", "default": "1",
             "help": "Les membres gagnent de l'XP en discutant."},
            {"key": "birthday_channel", "label": "Salon des anniversaires", "type": "channel",
             "help": "Le bot y souhaite les anniversaires enregistrés avec /anniversaire."},
            {"key": "star_channel", "label": "Salon des meilleurs messages", "type": "channel",
             "help": "Un message qui reçoit assez de ⭐ y est republié."},
            {"key": "star_threshold", "label": "Nombre de ⭐ nécessaires", "type": "choice", "default": "3",
             "options": [("2", "2"), ("3", "3"), ("5", "5"), ("10", "10")]},
        ],
    },
    "fivem": {
        "title": "FiveM & whitelist", "icon": "🎮",
        "desc": "Joueurs connectés en direct et candidatures de whitelist.",
        "fields": [
            {"key": "fivem_url", "label": "Adresse du serveur FiveM", "type": "text", "max": 200, "validate": "url",
             "help": "Ex. http://123.45.67.89:30120 — le bot affiche le nombre de joueurs dans son statut et avec /fivem."},
            {"key": "whitelist_channel", "label": "Salon des candidatures", "type": "channel",
             "help": "Salon réservé à l'équipe où arrivent les candidatures (boutons Accepter / Refuser)."},
            {"key": "whitelist_role", "label": "Rôle whitelist", "type": "role", "assignable": True,
             "help": "Donné automatiquement quand une candidature est acceptée."},
        ],
    },
}
ALL_FIELDS = [f for page in PAGES.values() for f in page["fields"]]

BOT_PERMISSIONS = dict(
    kick_members=True, ban_members=True, manage_channels=True, manage_roles=True,
    view_channel=True, send_messages=True, manage_messages=True, embed_links=True,
    attach_files=True, read_message_history=True, add_reactions=True, moderate_members=True,
    manage_guild=True, create_instant_invite=True,
)

PARTNERS_SQL = (
    "SELECT p.*, "
    "(SELECT COUNT(*) FROM partner_referrals r WHERE r.partner_id=p.id AND r.left_at IS NULL) AS active_count, "
    "(SELECT COUNT(*) FROM partner_referrals r WHERE r.partner_id=p.id) AS total_count "
    "FROM partners p WHERE p.guild_id=? AND p.active=1 ORDER BY active_count DESC, p.id"
)


YOUTUBE_RE = re.compile(r"^https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|embed/|live/)|youtu\.be/)([\w-]{6,20})")
VIMEO_RE = re.compile(r"^https?://(?:www\.)?vimeo\.com/(?:video/)?(\d{6,12})")
VIDEO_EXT = (".mp4", ".webm", ".mov", ".m4v", ".ogv")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
MAX_RESOURCES = 200
CHUNK = 1024 * 1024  # les fichiers sont stockés par morceaux de 1 Mo dans la base
ALLOWED_FILES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".ogv": "video/ogg",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".flac": "audio/flac",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".pdf": "application/pdf", ".zip": "application/zip", ".rar": "application/vnd.rar",
    ".7z": "application/x-7z-compressed", ".txt": "text/plain; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def file_kind(mime):
    mime = mime or ""
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("image/"):
        return "image"
    return "file"


def read_chunk(stream):
    """Lit exactement CHUNK octets (moins pour le dernier morceau)."""
    buffer = bytearray()
    while len(buffer) < CHUNK:
        part = stream.read(CHUNK - len(buffer))
        if not part:
            break
        buffer += part
    return bytes(buffer)


def env_int(name, default):
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def media_for(url):
    """Détermine comment afficher un lien : vidéo intégrée, vidéo directe, image ou simple lien."""
    match = YOUTUBE_RE.match(url)
    if match:
        return {"kind": "embed", "src": f"https://www.youtube-nocookie.com/embed/{match.group(1)}"}
    match = VIMEO_RE.match(url)
    if match:
        return {"kind": "embed", "src": f"https://player.vimeo.com/video/{match.group(1)}"}
    path = urllib.parse.urlparse(url).path.lower()
    if path.endswith(VIDEO_EXT):
        return {"kind": "video", "src": url}
    if path.endswith(IMAGE_EXT):
        return {"kind": "image", "src": url}
    return {"kind": "link", "src": url}


def valid_url(url):
    parsed = urllib.parse.urlparse(url)
    return len(url) <= 500 and parsed.scheme in ("http", "https") and bool(parsed.netloc)


def create_app(bot):
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
    max_upload_mb = env_int("MAX_UPLOAD_MB", 100)        # taille max d'un fichier
    storage_limit_mb = env_int("STORAGE_LIMIT_MB", 350)  # total max de fichiers dans la base (Neon gratuit : 512 Mo au total)
    app.config["MAX_CONTENT_LENGTH"] = (max_upload_mb + 2) * 1024 * 1024

    configured_url = os.getenv("DASHBOARD_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
    # Clé de session stable (dérivée du token) : on reste connecté après un redémarrage
    secret = os.getenv("DASHBOARD_SECRET") or hashlib.sha256(
        f"dashboard:{os.getenv('DISCORD_TOKEN', '')}".encode()
    ).hexdigest()
    app.secret_key = secret
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=configured_url.startswith("https"),
        PERMANENT_SESSION_LIFETIME=7 * 86400,
    )
    db.init()

    # ---------- Utilitaires ----------
    def base_url():
        return (os.getenv("DASHBOARD_URL") or os.getenv("RENDER_EXTERNAL_URL") or request.host_url).rstrip("/")

    def client_id():
        return os.getenv("DISCORD_CLIENT_ID") or str(bot.application_id or (bot.user.id if bot.user else ""))

    def can_manage(guild, user_id):
        member = guild.get_member(user_id)
        return bool(member) and (guild.owner_id == user_id or member.guild_permissions.manage_guild)

    def current_user_id():
        return int(session["user"]["id"])

    def guild_required(view):
        @wraps(view)
        def wrapper(guild_id, *args, **kwargs):
            if "user" not in session:
                return redirect(url_for("index"))
            guild = bot.get_guild(guild_id)
            if guild is None or not can_manage(guild, current_user_id()):
                abort(404)
            return view(guild, *args, **kwargs)
        return wrapper

    def select_options(guild):
        return {
            "channel": [(c.id, f"# {c.name}") for c in guild.text_channels],
            "category": [(c.id, c.name) for c in guild.categories],
            "role": [(r.id, r.name) for r in reversed(guild.roles) if not r.is_default()],
        }

    def check_role(guild, role_id, assignable=True):
        """Retourne (rôle, erreur)."""
        role = guild.get_role(role_id)
        if role is None or role.is_default():
            return None, "Rôle introuvable."
        if assignable and role >= guild.me.top_role:
            return None, f"Le rôle « {role.name} » est au-dessus de celui du bot : il ne pourra pas le donner. Monte le rôle du bot dans les paramètres du serveur."
        return role, None

    def can_view(guild, member, role_id):
        """Un gestionnaire voit tout ; un membre doit avoir le rôle de la ressource (ou le rôle global)."""
        if member is None:
            return False
        if guild.owner_id == member.id or member.guild_permissions.manage_guild:
            return True
        needed = role_id or db.get_int(guild.id, "resources_role")
        return bool(needed) and any(role.id == needed for role in member.roles)

    def accessible_guilds(uid):
        result = []
        for g in sorted(bot.guilds, key=lambda x: x.name.lower()):
            member = g.get_member(uid)
            if member is None:
                continue
            rows = db.rows("SELECT role_id FROM resources WHERE guild_id=?", (g.id,))
            count = sum(1 for r in rows if can_view(g, member, r["role_id"]))
            if count:
                result.append({"guild": g, "count": count})
        return result

    def resource_media(row):
        """Comment afficher une ressource : fichier hébergé sur le site ou lien externe."""
        if row["file_name"]:
            src = url_for("resource_file", resource_id=row["id"])
            return {
                "kind": file_kind(row["file_type"]), "src": src, "download": src + "?dl=1",
                "name": row["file_name"], "size": int(row["file_size"] or 0), "hosted": True,
            }
        media = media_for(row["url"])
        media["hosted"] = False
        return media

    def storage_used():
        return int(db.one("SELECT COALESCE(SUM(file_size), 0) AS s FROM resources")["s"] or 0)

    def log_action(guild, action, detail=""):
        user = session.get("user") or {}
        audit.record(guild.id, user.get("id"), user.get("name"), action, detail)

    def tools_cog():
        cog = bot.get_cog("Tools")
        if cog is None:
            raise RuntimeError("Le module Tools n'est pas chargé sur le bot.")
        return cog

    def run_in_bot(coro, timeout=25):
        """Exécute une action du bot (Discord) depuis le site et attend le résultat."""
        return asyncio.run_coroutine_threadsafe(coro, bot.loop).result(timeout=timeout)

    def partners_cog():
        cog = bot.get_cog("Partners")
        if cog is None:
            raise RuntimeError("Le module Partenaires n'est pas chargé sur le bot.")
        return cog

    def partner_guilds(uid):
        result = []
        for row in db.rows("SELECT guild_id, name FROM partners WHERE user_id=? AND active=1", (uid,)):
            g = bot.get_guild(row["guild_id"])
            if g is not None and g.get_member(uid) is not None:
                result.append({"guild": g, "name": row["name"]})
        return result

    def partner_view(guild, partner):
        """Toutes les données affichées dans l'espace d'un partenaire."""
        tiers = db.rows("SELECT * FROM partner_tiers WHERE guild_id=? ORDER BY min_members", (guild.id,))
        counts = db.one(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN left_at IS NULL THEN 1 ELSE 0 END) AS active "
            "FROM partner_referrals WHERE partner_id=?", (partner["id"],),
        )
        active, total = int(counts["active"] or 0), int(counts["total"] or 0)
        current, upcoming, percent = tier_progress(tiers, active)

        cards = []
        for t in tiers:
            if active >= t["min_members"]:
                state = "done"
            elif upcoming and t["id"] == upcoming["id"]:
                state = "next"
            else:
                state = "locked"
            role = guild.get_role(t["role_id"]) if t["role_id"] else None
            cards.append({
                "name": t["name"], "min": t["min_members"], "reward": t["reward"],
                "role": role.name if role else None, "state": state,
                "current": bool(current and t["id"] == current["id"]),
            })

        referrals = []
        for r in db.rows("SELECT * FROM partner_referrals WHERE partner_id=? ORDER BY id DESC LIMIT 30", (partner["id"],)):
            member = guild.get_member(r["member_id"])
            referrals.append({
                "name": member.display_name if member else "Ancien membre",
                "joined_at": r["joined_at"], "left": r["left_at"] is not None,
            })
        board = [
            {"name": row["name"], "count": int(row["active_count"] or 0), "me": row["id"] == partner["id"]}
            for row in db.rows(PARTNERS_SQL + " LIMIT 10", (guild.id,))
        ]
        return {
            "partner": partner, "active": active, "total": total, "left": total - active,
            "current": {"name": current["name"]} if current else None,
            "upcoming": {
                "name": upcoming["name"], "min": upcoming["min_members"], "remaining": upcoming["min_members"] - active,
            } if upcoming else None,
            "percent": percent, "tiers": cards, "referrals": referrals, "board": board,
            "invite_url": f"https://discord.gg/{partner['invite_code']}",
        }

    # ---------- Filtres et contexte ----------
    @app.template_filter("datefmt")
    def datefmt(ts):
        if not ts:
            return "—"
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%d/%m/%Y %H:%M") + " UTC"

    @app.template_filter("euros")
    def euros_filter(cents):
        return f"{int(cents or 0) / 100:.2f} €".replace(".", ",")

    @app.template_filter("stars")
    def stars(n):
        n = int(n)
        return "⭐" * n + "☆" * (5 - n)

    @app.context_processor
    def inject():
        uid = (session.get("user") or {}).get("id")
        is_partner = bool(uid) and db.one(
            "SELECT 1 FROM partners WHERE user_id=? AND active=1", (int(uid),)
        ) is not None
        return {
            "is_partner": is_partner,
            "brand": os.getenv("BRAND_NAME") or (bot.user.name if bot.user else "Bot"),
            "logo": os.getenv("LOGO_URL") or None,
            "user": session.get("user"),
            "csrf": session.get("csrf", ""),
            "pages": PAGES,
        }

    @app.before_request
    def check_csrf():
        if request.method == "POST":
            if request.path == "/webhooks/tebex":
                return  # protégé par la signature Tebex
            if "user" not in session:
                abort(400)  # on ne lit pas un gros envoi venant de quelqu'un qui n'est pas connecté
            sent, expected = request.form.get("csrf", ""), session.get("csrf", "")
            if not expected or not secrets.compare_digest(sent, expected):
                abort(400)

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def handle_error(error):
        messages = {
            400: "Requête invalide ou session expirée. Recharge la page et reconnecte-toi.",
            403: "Accès refusé.",
            404: "Page introuvable, ou tu n'as pas la permission « Gérer le serveur » sur ce serveur.",
            413: f"Fichier trop volumineux (maximum {max_upload_mb} Mo par fichier).",
        }
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify(ok=False, message=messages.get(error.code, "Erreur.")), error.code
        return render_template("error.html", title=f"Erreur {error.code}", message=messages.get(error.code, "Erreur.")), error.code

    # ---------- Connexion Discord ----------
    @app.route("/")
    def index():
        if "user" in session:
            return redirect(url_for("servers"))
        return render_template("index.html", configured=bool(os.getenv("DISCORD_CLIENT_SECRET")))

    @app.route("/login")
    def login():
        if not os.getenv("DISCORD_CLIENT_SECRET"):
            return render_template(
                "error.html", title="Dashboard non configuré",
                message="La variable DISCORD_CLIENT_SECRET est absente. Ajoute-la dans l'environnement de ton hébergeur.",
            ), 503
        state = secrets.token_urlsafe(16)
        session["oauth_state"] = state
        wanted = request.args.get("next", "")
        session["next"] = wanted if wanted.startswith("/") and not wanted.startswith("//") else ""
        params = {
            "client_id": client_id(),
            "redirect_uri": f"{base_url()}/callback",
            "response_type": "code",
            "scope": "identify",
            "state": state,
        }
        return redirect("https://discord.com/oauth2/authorize?" + urllib.parse.urlencode(params))

    @app.route("/callback")
    def callback():
        expected = session.pop("oauth_state", None)
        next_url = session.pop("next", "")
        code = request.args.get("code")
        if not expected or request.args.get("state") != expected or not code:
            abort(400)
        data = urllib.parse.urlencode({
            "client_id": client_id(),
            "client_secret": os.getenv("DISCORD_CLIENT_SECRET", ""),
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"{base_url()}/callback",
        }).encode()
        try:
            req = urllib.request.Request(
                f"{API}/oauth2/token", data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                access_token = json.load(response)["access_token"]
            req = urllib.request.Request(
                f"{API}/users/@me",
                headers={"Authorization": f"Bearer {access_token}", "User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                me = json.load(response)
        except (urllib.error.URLError, KeyError, ValueError):
            return render_template(
                "error.html", title="Connexion impossible",
                message="Discord a refusé la connexion. Vérifie DISCORD_CLIENT_SECRET et l'adresse de redirection ajoutée dans le portail développeur.",
            ), 502

        avatar = (
            f"https://cdn.discordapp.com/avatars/{me['id']}/{me['avatar']}.png?size=64"
            if me.get("avatar")
            else f"https://cdn.discordapp.com/embed/avatars/{(int(me['id']) >> 22) % 6}.png"
        )
        session.clear()
        session.permanent = True
        session["user"] = {"id": str(me["id"]), "name": me.get("global_name") or me["username"], "avatar": avatar}
        session["csrf"] = secrets.token_hex(16)
        return redirect(next_url or url_for("servers"))

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    # ---------- Liste des serveurs ----------
    @app.route("/dashboard")
    def servers():
        if "user" not in session:
            return redirect(url_for("index"))
        uid = current_user_id()
        guilds = sorted((g for g in bot.guilds if can_manage(g, uid)), key=lambda g: g.name.lower())
        invite = "https://discord.com/oauth2/authorize?" + urllib.parse.urlencode({
            "client_id": client_id(),
            "scope": "bot applications.commands",
            "permissions": discord.Permissions(**BOT_PERMISSIONS).value,
        })
        return render_template(
            "servers.html", guilds=guilds, invite=invite,
            resource_guilds=accessible_guilds(uid), partner_guilds=partner_guilds(uid),
        )

    # ---------- Vue d'ensemble ----------
    @app.route("/g/<int:guild_id>")
    @guild_required
    def overview(guild):
        gid = guild.id
        total = db.one("SELECT COUNT(*) AS c FROM tickets WHERE guild_id=?", (gid,))["c"]
        opened = db.one("SELECT COUNT(*) AS c FROM tickets WHERE guild_id=? AND closed_at IS NULL", (gid,))["c"]
        rating = db.one("SELECT AVG(rating) AS a, COUNT(*) AS c FROM reviews WHERE guild_id=?", (gid,))
        by_status = {
            r["status"]: r["c"]
            for r in db.rows("SELECT status, COUNT(*) AS c FROM tickets WHERE guild_id=? GROUP BY status", (gid,))
        }
        cog = bot.get_cog("Giveaways")
        giveaways = sum(1 for g in getattr(cog, "active", {}).values() if g.guild_id == gid) if cog else 0

        top = []
        for r in db.rows("SELECT user_id, xp FROM levels WHERE guild_id=? ORDER BY xp DESC LIMIT 5", (gid,)):
            member = guild.get_member(r["user_id"])
            top.append({
                "name": member.display_name if member else str(r["user_id"]),
                "xp": r["xp"], "level": level_progress(r["xp"])[0],
            })

        core = [f for f in ALL_FIELDS if f.get("core")]
        missing = [f["label"] for f in core if not db.get_config(gid, f["key"])]
        return render_template(
            "overview.html", guild=guild, section="overview",
            stats={
                "members": guild.member_count, "total": total, "opened": opened, "giveaways": giveaways,
                "rating": rating["a"], "reviews": rating["c"],
            },
            status_counts=[(order_status.label(k), by_status.get(k, 0)) for k in order_status.STATUSES],
            top=top, recent=ticket_rows(guild, 6),
            setup_done=len(core) - len(missing), setup_total=len(core), missing=missing,
        )

    def ticket_rows(guild, limit):
        rows = []
        for r in db.rows("SELECT * FROM tickets WHERE guild_id=? ORDER BY id DESC LIMIT ?", (guild.id, limit)):
            member = guild.get_member(r["user_id"])
            claimed = guild.get_member(r["claimed_by"]) if r["claimed_by"] else None
            rows.append({
                "number": r["number"],
                "category": r["category"].capitalize(),
                "client": member.display_name if member else str(r["user_id"]),
                "status": order_status.label(r["status"]),
                "created_at": r["created_at"],
                "open": r["closed_at"] is None,
                "claimed_by": claimed.display_name if claimed else None,
            })
        return rows

    # ---------- Réglages ----------
    def save_page(guild, spec, form):
        errors, updates = [], {}
        for f in spec["fields"]:
            key, kind = f["key"], f["type"]
            if kind == "toggle":
                updates[key] = "1" if key in form else "0"
                continue
            raw = form.get(key, "").strip()
            if kind == "text":
                if f.get("validate") == "url" and raw and not valid_url(raw):
                    errors.append(f"« {f['label']} » doit commencer par http:// ou https://.")
                    continue
                updates[key] = raw[: f.get("max", 500)] or None
            elif kind == "choice":
                allowed = {value for value, _ in f["options"]}
                if raw in allowed:
                    updates[key] = raw
                else:
                    errors.append(f"Valeur invalide pour « {f['label']} ».")
            else:  # channel | category | role
                if not raw:
                    updates[key] = None
                    continue
                if not raw.isdigit():
                    errors.append(f"Valeur invalide pour « {f['label']} ».")
                    continue
                ident = int(raw)
                if kind == "channel":
                    ok = isinstance(guild.get_channel(ident), discord.TextChannel)
                elif kind == "category":
                    ok = isinstance(guild.get_channel(ident), discord.CategoryChannel)
                else:
                    _, problem = check_role(guild, ident, f.get("assignable", False))
                    ok = problem is None
                    if problem:
                        errors.append(f"{f['label']} : {problem}")
                        continue
                if ok:
                    updates[key] = ident
                else:
                    errors.append(f"« {f['label']} » : élément introuvable sur ce serveur.")
        if errors:
            return errors
        for key, value in updates.items():
            if value is None:
                db.delete_config(guild.id, key)
            else:
                db.set_config(guild.id, key, value)
        return []

    @app.route("/g/<int:guild_id>/settings/<page>", methods=["GET", "POST"])
    @guild_required
    def settings(guild, page):
        spec = PAGES.get(page)
        if spec is None:
            abort(404)
        if request.method == "POST":
            before = {f["key"]: db.get_config(guild.id, f["key"]) for f in spec["fields"]}
            errors = save_page(guild, spec, request.form)
            for error in errors:
                flash(error, "error")
            if not errors:
                flash("Réglages enregistrés ✅", "success")
                changed = [f["label"] for f in spec["fields"] if db.get_config(guild.id, f["key"]) != before[f["key"]]]
                if changed:
                    log_action(guild, f"Réglages « {spec['title']} » modifiés", ", ".join(changed))
            return redirect(url_for("settings", guild_id=guild.id, page=page))

        values = {f["key"]: db.get_config(guild.id, f["key"], f.get("default")) for f in spec["fields"]}
        rewards = []
        if page == "community":
            for r in db.rows("SELECT level, role_id FROM level_roles WHERE guild_id=? ORDER BY level", (guild.id,)):
                role = guild.get_role(r["role_id"])
                rewards.append({"level": r["level"], "role": role.name if role else "(rôle supprimé)"})
        return render_template(
            "settings.html", guild=guild, section=page, page=page, spec=spec,
            values=values, options=select_options(guild), rewards=rewards,
        )

    @app.route("/g/<int:guild_id>/levels/add", methods=["POST"])
    @guild_required
    def level_add(guild):
        level, role_raw = request.form.get("level", ""), request.form.get("role", "")
        if not (level.isdigit() and 1 <= int(level) <= 200 and role_raw.isdigit()):
            flash("Niveau (1 à 200) ou rôle invalide.", "error")
        else:
            role, problem = check_role(guild, int(role_raw), assignable=True)
            if problem:
                flash(problem, "error")
            else:
                db.run(
                    "INSERT INTO level_roles (guild_id, level, role_id) VALUES (?, ?, ?) "
                    "ON CONFLICT(guild_id, level) DO UPDATE SET role_id=excluded.role_id",
                    (guild.id, int(level), role.id),
                )
                flash(f"Niveau {level} : rôle « {role.name} » ajouté ✅", "success")
                log_action(guild, "Récompense de niveau ajoutée", f"Niveau {level} → {role.name}")
        return redirect(url_for("settings", guild_id=guild.id, page="community"))

    @app.route("/g/<int:guild_id>/levels/delete", methods=["POST"])
    @guild_required
    def level_delete(guild):
        level = request.form.get("level", "")
        if level.isdigit():
            db.run("DELETE FROM level_roles WHERE guild_id=? AND level=?", (guild.id, int(level)))
            flash("Récompense retirée.", "success")
            log_action(guild, "Récompense de niveau retirée", f"Niveau {level}")
        return redirect(url_for("settings", guild_id=guild.id, page="community"))

    # ---------- FAQ ----------
    @app.route("/g/<int:guild_id>/faq")
    @guild_required
    def faq(guild):
        items = db.rows("SELECT * FROM faq WHERE guild_id=? ORDER BY id", (guild.id,))
        return render_template("faq.html", guild=guild, section="faq", items=items)

    @app.route("/g/<int:guild_id>/faq/add", methods=["POST"])
    @guild_required
    def faq_add(guild):
        question = request.form.get("question", "").strip()
        answer = request.form.get("answer", "").strip()
        if not question or not answer:
            flash("La question et la réponse sont obligatoires.", "error")
        elif len(question) > 100 or len(answer) > 1500:
            flash("Question : 100 caractères max. Réponse : 1500 caractères max.", "error")
        elif db.one("SELECT COUNT(*) AS c FROM faq WHERE guild_id=?", (guild.id,))["c"] >= 25:
            flash("La FAQ est limitée à 25 questions (limite de Discord pour un menu).", "error")
        else:
            db.run("INSERT INTO faq (guild_id, question, answer) VALUES (?, ?, ?)", (guild.id, question, answer))
            flash("Question ajoutée ✅", "success")
            log_action(guild, "Question FAQ ajoutée", question)
        return redirect(url_for("faq", guild_id=guild.id))

    @app.route("/g/<int:guild_id>/faq/delete", methods=["POST"])
    @guild_required
    def faq_delete(guild):
        faq_id = request.form.get("id", "")
        if faq_id.isdigit():
            db.run("DELETE FROM faq WHERE id=? AND guild_id=?", (int(faq_id), guild.id))
            flash("Question supprimée.", "success")
            log_action(guild, "Question FAQ supprimée", f"ID {faq_id}")
        return redirect(url_for("faq", guild_id=guild.id))

    # ---------- Activité ----------
    @app.route("/g/<int:guild_id>/activity")
    @guild_required
    def activity(guild):
        reviews = []
        for r in db.rows("SELECT * FROM reviews WHERE guild_id=? ORDER BY id DESC LIMIT 10", (guild.id,)):
            member = guild.get_member(r["user_id"])
            reviews.append({
                "client": member.display_name if member else str(r["user_id"]),
                "rating": r["rating"], "comment": r["comment"], "created_at": r["created_at"],
                "number": r["ticket_number"],
            })
        return render_template(
            "activity.html", guild=guild, section="activity",
            tickets=ticket_rows(guild, 50), reviews=reviews,
        )

    # ---------- Ressources : gestion (administrateurs) ----------
    @app.route("/g/<int:guild_id>/resources")
    @guild_required
    def resources_admin(guild):
        items = []
        for r in db.rows("SELECT * FROM resources WHERE guild_id=? ORDER BY id DESC", (guild.id,)):
            role = guild.get_role(r["role_id"]) if r["role_id"] else None
            items.append({
                "id": r["id"], "title": r["title"], "url": r["url"],
                "category": r["category"] or "Général",
                "kind": resource_media(r)["kind"], "hosted": bool(r["file_name"]),
                "file_name": r["file_name"], "size": int(r["file_size"] or 0),
                "role": role.name if role else ("(rôle supprimé)" if r["role_id"] else "Rôle global"),
                "created_at": r["created_at"],
            })
        return render_template(
            "resources_admin.html", guild=guild, section="resources", items=items,
            categories=sorted({i["category"] for i in items}), options=select_options(guild),
            current_role=db.get_int(guild.id, "resources_role"),
            share_url=base_url() + url_for("member_resources", guild_id=guild.id),
            max_upload_mb=max_upload_mb, storage_limit_mb=storage_limit_mb, used_mb=storage_used() / 1048576,
        )

    @app.route("/g/<int:guild_id>/resources/role", methods=["POST"])
    @guild_required
    def resources_role(guild):
        raw = request.form.get("role", "").strip()
        if not raw:
            db.delete_config(guild.id, "resources_role")
            flash("Rôle d'accès retiré : seuls les gestionnaires voient les ressources.", "success")
            log_action(guild, "Accès aux ressources", "rôle retiré")
        elif not raw.isdigit():
            flash("Rôle invalide.", "error")
        else:
            role, problem = check_role(guild, int(raw), assignable=False)
            if problem:
                flash(problem, "error")
            else:
                db.set_config(guild.id, "resources_role", role.id)
                flash(f"Rôle d'accès : « {role.name} » ✅", "success")
                log_action(guild, "Accès aux ressources", f"rôle : {role.name}")
        return redirect(url_for("resources_admin", guild_id=guild.id))

    @app.route("/g/<int:guild_id>/resources/add", methods=["POST"])
    @guild_required
    def resources_add(guild):
        title = request.form.get("title", "").strip()
        url = request.form.get("url", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "").strip()
        role_raw = request.form.get("role", "").strip()
        upload = request.files.get("file")
        has_file = bool(upload and upload.filename)
        ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

        def done(ok, message):
            if ajax:
                return jsonify(ok=ok, message=message)
            flash(message, "success" if ok else "error")
            return redirect(url_for("resources_admin", guild_id=guild.id))

        if not title or len(title) > 100:
            return done(False, "Le titre est obligatoire (100 caractères max).")
        if has_file and url:
            return done(False, "Choisis soit un fichier, soit un lien, pas les deux.")
        if not has_file and not valid_url(url):
            return done(False, "Ajoute un fichier, ou un lien commençant par http:// ou https://.")
        if len(description) > 500 or len(category) > 40:
            return done(False, "Description : 500 caractères max. Catégorie : 40 caractères max.")
        if db.one("SELECT COUNT(*) AS c FROM resources WHERE guild_id=?", (guild.id,))["c"] >= MAX_RESOURCES:
            return done(False, f"Limite de {MAX_RESOURCES} ressources atteinte.")

        role_id = None
        if role_raw:
            role, problem = check_role(guild, int(role_raw), assignable=False) if role_raw.isdigit() else (None, "Rôle invalide.")
            if problem:
                return done(False, problem)
            role_id = role.id

        name = mime = None
        size = 0
        if has_file:
            name = os.path.basename(upload.filename)[:150]
            mime = ALLOWED_FILES.get(os.path.splitext(name)[1].lower())
            if mime is None:
                return done(False, "Type de fichier non autorisé. Formats acceptés : vidéo (mp4, webm, mov), audio (mp3, wav, ogg, m4a), "
                                   "image (png, jpg, gif, webp), pdf, zip, rar, 7z, txt, docx, xlsx, pptx.")
            upload.stream.seek(0, os.SEEK_END)
            size = upload.stream.tell()
            upload.stream.seek(0)
            if size == 0:
                return done(False, "Le fichier est vide.")
            if size > max_upload_mb * 1048576:
                return done(False, f"Fichier trop lourd ({size / 1048576:.1f} Mo) : maximum {max_upload_mb} Mo par fichier.")
            if storage_used() + size > storage_limit_mb * 1048576:
                return done(False, f"Espace de stockage presque plein ({storage_used() / 1048576:.0f} Mo sur {storage_limit_mb} Mo). "
                                   "Supprime des fichiers ou utilise un lien.")

        resource_id, _ = db.run(
            "INSERT INTO resources (guild_id, title, description, url, category, role_id, created_by, created_at, "
            "file_name, file_type, file_size) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild.id, title, description or None, "" if has_file else url, category or None, role_id,
             current_user_id(), int(datetime.now(timezone.utc).timestamp()), name, mime, size or None),
        )
        if has_file:
            try:
                written, index = 0, 0
                while True:
                    chunk = read_chunk(upload.stream)
                    if not chunk:
                        break
                    db.run(
                        "INSERT INTO resource_files (resource_id, chunk_index, data) VALUES (?, ?, ?)",
                        (resource_id, index, chunk),
                    )
                    written += len(chunk)
                    index += 1
                if written != size:
                    raise IOError("Taille du fichier incohérente")
                db.run("UPDATE resources SET url=? WHERE id=?", (f"/f/{resource_id}", resource_id))
            except Exception:
                logging.exception("Enregistrement du fichier impossible")
                try:
                    db.run("DELETE FROM resource_files WHERE resource_id=?", (resource_id,))
                    db.run("DELETE FROM resources WHERE id=?", (resource_id,))
                except Exception:
                    logging.exception("Nettoyage impossible")
                return done(False, "L'envoi du fichier a échoué. Réessaie.")
            log_action(guild, "Ressource ajoutée", f"{title} (fichier, {size / 1048576:.1f} Mo)")
            return done(True, f"« {title} » ajouté ✅ ({size / 1048576:.1f} Mo)")
        log_action(guild, "Ressource ajoutée", f"{title} (lien)")
        return done(True, "Ressource ajoutée ✅")

    @app.route("/g/<int:guild_id>/resources/delete", methods=["POST"])
    @guild_required
    def resources_delete(guild):
        resource_id = request.form.get("id", "")
        if resource_id.isdigit():
            owned = db.one("SELECT 1 FROM resources WHERE id=? AND guild_id=?", (int(resource_id), guild.id))
            if owned:
                db.run("DELETE FROM resource_files WHERE resource_id=?", (int(resource_id),))
                db.run("DELETE FROM resources WHERE id=? AND guild_id=?", (int(resource_id), guild.id))
            flash("Ressource supprimée.", "success")
            log_action(guild, "Ressource supprimée", f"ID {resource_id}")
        return redirect(url_for("resources_admin", guild_id=guild.id))

    # ---------- Ressources : espace membres ----------
    @app.route("/f/<int:resource_id>")
    def resource_file(resource_id):
        if "user" not in session:
            return redirect(url_for("login", next=request.path))
        row = db.one("SELECT * FROM resources WHERE id=? AND file_name IS NOT NULL", (resource_id,))
        guild = bot.get_guild(row["guild_id"]) if row else None
        member = guild.get_member(current_user_id()) if guild else None
        if row is None or member is None or not can_view(guild, member, row["role_id"]):
            abort(404)

        size = int(row["file_size"])
        start, end, status_code = 0, size - 1, 200
        range_header = request.headers.get("Range", "")
        if range_header:
            match = RANGE_RE.match(range_header.strip())
            if match and (match.group(1) or match.group(2)):
                if match.group(1):
                    start = int(match.group(1))
                    end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
                else:  # « les N derniers octets »
                    start, end = max(size - int(match.group(2)), 0), size - 1
                if start > end or start >= size:
                    return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
                status_code = 206

        def generate():
            position = start
            while position <= end:
                index = position // CHUNK
                chunk = db.one("SELECT data FROM resource_files WHERE resource_id=? AND chunk_index=?", (resource_id, index))
                if chunk is None:
                    break
                data = bytes(chunk["data"])
                piece = data[position - index * CHUNK: position - index * CHUNK + (end - position + 1)]
                if not piece:
                    break
                yield piece
                position += len(piece)

        mime = row["file_type"] or "application/octet-stream"
        previewable = file_kind(mime) != "file" and request.args.get("dl") != "1"
        safe_name = re.sub(r'[^A-Za-z0-9._-]', "_", row["file_name"]) or "fichier"
        headers = {
            "Content-Length": str(end - start + 1),
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": (
                f"{'inline' if previewable else 'attachment'}; filename=\"{safe_name}\"; "
                f"filename*=UTF-8''{urllib.parse.quote(row['file_name'])}"
            ),
        }
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return Response(generate(), status=status_code, mimetype=mime, headers=headers)

    @app.route("/ressources")
    def resources_hub():
        if "user" not in session:
            return redirect(url_for("login", next=request.path))
        return render_template("member_hub.html", cards=accessible_guilds(current_user_id()))

    @app.route("/r/<int:guild_id>")
    def member_resources(guild_id):
        if "user" not in session:
            return redirect(url_for("login", next=request.path))
        guild = bot.get_guild(guild_id)
        member = guild.get_member(current_user_id()) if guild else None
        if member is None:
            abort(404)  # on ne révèle rien aux personnes qui ne sont pas sur le serveur

        rows = db.rows("SELECT * FROM resources WHERE guild_id=? ORDER BY id DESC", (guild.id,))
        groups = {}
        for r in rows:
            if can_view(guild, member, r["role_id"]):
                groups.setdefault(r["category"] or "Général", []).append({
                    "title": r["title"], "description": r["description"], "media": resource_media(r),
                })
        groups = dict(sorted(groups.items(), key=lambda kv: kv[0].lower()))

        locked_role = None
        if rows and not groups:
            role = guild.get_role(db.get_int(guild.id, "resources_role") or 0)
            if role:
                locked_role = role.name
        is_manager = can_view(guild, member, None) and (
            guild.owner_id == member.id or member.guild_permissions.manage_guild
        )
        return render_template(
            "member_resources.html", guild=guild, groups=groups, locked_role=locked_role,
            is_manager=is_manager, empty=not rows,
        )

    # ---------- Partenaires : gestion (administrateurs) ----------
    @app.route("/g/<int:guild_id>/partners")
    @guild_required
    def partners_admin(guild):
        tiers = db.rows("SELECT * FROM partner_tiers WHERE guild_id=? ORDER BY min_members", (guild.id,))
        tier_names = {t["id"]: t["name"] for t in tiers}
        tier_list = []
        for t in tiers:
            role = guild.get_role(t["role_id"]) if t["role_id"] else None
            tier_list.append({
                "id": t["id"], "name": t["name"], "min": t["min_members"], "reward": t["reward"],
                "role": role.name if role else None,
            })
        partners = []
        for r in db.rows(PARTNERS_SQL, (guild.id,)):
            member = guild.get_member(r["user_id"])
            partners.append({
                "id": r["id"], "name": r["name"],
                "user": member.display_name if member else str(r["user_id"]),
                "invite_url": f"https://discord.gg/{r['invite_code']}",
                "active": int(r["active_count"] or 0), "total": int(r["total_count"] or 0),
                "tier": tier_names.get(r["tier_id"]),
            })
        members = sorted(((m.id, m.display_name) for m in guild.members if not m.bot), key=lambda x: x[1].lower())[:1000]
        can_track = bool(getattr(getattr(guild.me, "guild_permissions", None), "manage_guild", True))
        return render_template(
            "partners_admin.html", guild=guild, section="partners", tiers=tier_list, partners=partners,
            members=members, options=select_options(guild), can_track=can_track,
            default_channel=db.get_int(guild.id, "partner_invite_channel"),
            share_url=base_url() + url_for("partner_hub"),
            min_age=db.get_config(guild.id, "partner_min_age_days", "0"),
            board_channel=db.get_int(guild.id, "partner_board_channel"),
        )

    @app.route("/g/<int:guild_id>/partners/<int:partner_id>")
    @guild_required
    def partner_admin_view(guild, partner_id):
        partner = db.one("SELECT * FROM partners WHERE id=? AND guild_id=? AND active=1", (partner_id, guild.id))
        if partner is None:
            abort(404)
        return render_template("partner_space.html", guild=guild, admin_view=True, **partner_view(guild, partner))

    @app.route("/g/<int:guild_id>/partners/tier/add", methods=["POST"])
    @guild_required
    def partner_tier_add(guild):
        name = request.form.get("name", "").strip()
        min_raw = request.form.get("min_members", "").strip()
        reward = request.form.get("reward", "").strip()
        role_raw = request.form.get("role", "").strip()
        role_id, error = None, None
        if not name or len(name) > 40:
            error = "Le nom du palier est obligatoire (40 caractères max)."
        elif not (min_raw.isdigit() and 1 <= int(min_raw) <= 100000):
            error = "Le nombre de membres doit être compris entre 1 et 100000."
        elif len(reward) > 300:
            error = "Récompense : 300 caractères max."
        elif db.one("SELECT COUNT(*) AS c FROM partner_tiers WHERE guild_id=?", (guild.id,))["c"] >= 15:
            error = "Limite de 15 paliers atteinte."
        elif db.one("SELECT 1 FROM partner_tiers WHERE guild_id=? AND min_members=?", (guild.id, int(min_raw))):
            error = "Un palier existe déjà pour ce nombre de membres."
        elif role_raw:
            role, problem = check_role(guild, int(role_raw), assignable=True) if role_raw.isdigit() else (None, "Rôle invalide.")
            if problem:
                error = problem
            else:
                role_id = role.id
        if error:
            flash(error, "error")
        else:
            db.run(
                "INSERT INTO partner_tiers (guild_id, name, min_members, reward, role_id) VALUES (?, ?, ?, ?, ?)",
                (guild.id, name, int(min_raw), reward or None, role_id),
            )
            try:
                run_in_bot(partners_cog().refresh_guild(guild.id))
            except Exception:
                logging.exception("Mise à jour des paliers impossible")
            flash(f"Palier « {name} » ajouté ✅", "success")
            log_action(guild, "Palier partenaire ajouté", f"{name} ({min_raw} membres)")
        return redirect(url_for("partners_admin", guild_id=guild.id))

    @app.route("/g/<int:guild_id>/partners/tier/delete", methods=["POST"])
    @guild_required
    def partner_tier_delete(guild):
        tier_id = request.form.get("id", "")
        if tier_id.isdigit():
            try:
                run_in_bot(partners_cog().delete_tier(guild.id, int(tier_id)))
            except Exception:
                logging.exception("Suppression du palier via le bot impossible")
                db.run("DELETE FROM partner_tiers WHERE id=? AND guild_id=?", (int(tier_id), guild.id))
            flash("Palier supprimé.", "success")
            log_action(guild, "Palier partenaire supprimé", f"ID {tier_id}")
        return redirect(url_for("partners_admin", guild_id=guild.id))

    @app.route("/g/<int:guild_id>/partners/add", methods=["POST"])
    @guild_required
    def partner_add(guild):
        user_raw = request.form.get("user_id", "").strip() or request.form.get("user", "").strip()
        channel_raw = request.form.get("channel", "").strip()
        name = request.form.get("name", "").strip()
        member, error = None, None
        if not user_raw.isdigit():
            error = "Choisis un membre du serveur (ou colle son ID Discord)."
        else:
            member = guild.get_member(int(user_raw))
            if member is None or member.bot:
                error = "Ce membre est introuvable sur le serveur."
            elif db.one("SELECT 1 FROM partners WHERE guild_id=? AND user_id=? AND active=1", (guild.id, member.id)):
                error = "Cette personne est déjà partenaire."
            elif not channel_raw.isdigit() or not isinstance(guild.get_channel(int(channel_raw)), discord.TextChannel):
                error = "Choisis le salon dans lequel l'invitation sera créée."
            elif db.one("SELECT COUNT(*) AS c FROM partners WHERE guild_id=? AND active=1", (guild.id,))["c"] >= 100:
                error = "Limite de 100 partenaires atteinte."
        if error:
            flash(error, "error")
            return redirect(url_for("partners_admin", guild_id=guild.id))

        name = name[:40] or member.display_name[:40]
        try:
            code = run_in_bot(partners_cog().create_partner(guild.id, member.id, name, int(channel_raw)))
        except discord.Forbidden:
            flash("Le bot n'a pas la permission de créer une invitation dans ce salon "
                  "(il lui faut « Créer une invitation » et « Gérer le serveur »).", "error")
        except Exception:
            logging.exception("Création du lien partenaire impossible")
            flash("Impossible de créer le lien d'invitation pour le moment. Réessaie dans un instant.", "error")
        else:
            db.set_config(guild.id, "partner_invite_channel", channel_raw)
            flash(f"Partenaire « {name} » ajouté ✅ Son lien : https://discord.gg/{code}", "success")
            log_action(guild, "Partenaire ajouté", f"{name} (compte {member.display_name})")
        return redirect(url_for("partners_admin", guild_id=guild.id))

    @app.route("/g/<int:guild_id>/partners/delete", methods=["POST"])
    @guild_required
    def partner_delete(guild):
        partner_id = request.form.get("id", "")
        if partner_id.isdigit():
            try:
                run_in_bot(partners_cog().remove_partner(guild.id, int(partner_id)))
                flash("Partenaire retiré : son lien d'invitation a été supprimé.", "success")
                log_action(guild, "Partenaire retiré", f"ID {partner_id}")
            except Exception:
                logging.exception("Retrait du partenaire impossible")
                flash("Impossible de retirer ce partenaire pour le moment.", "error")
        return redirect(url_for("partners_admin", guild_id=guild.id))

    # ---------- Partenaires : espace du partenaire ----------
    @app.route("/partenaire")
    def partner_hub():
        if "user" not in session:
            return redirect(url_for("login", next=request.path))
        cards = partner_guilds(current_user_id())
        if len(cards) == 1:
            return redirect(url_for("partner_space", guild_id=cards[0]["guild"].id))
        return render_template("partner_hub.html", cards=cards)

    @app.route("/p/<int:guild_id>")
    def partner_space(guild_id):
        if "user" not in session:
            return redirect(url_for("login", next=request.path))
        guild = bot.get_guild(guild_id)
        uid = current_user_id()
        member = guild.get_member(uid) if guild else None
        partner = (
            db.one("SELECT * FROM partners WHERE guild_id=? AND user_id=? AND active=1", (guild_id, uid))
            if member else None
        )
        if partner is None:
            abort(404)  # seul le partenaire concerné peut ouvrir son espace
        return render_template("partner_space.html", guild=guild, admin_view=False, **partner_view(guild, partner))

    # ---------- Statistiques ----------
    def day_chart(title, timestamps, start_day, color):
        counts = {d: 0 for d in range(start_day, start_day + 30)}
        for ts in timestamps:
            if ts // 86400 in counts:
                counts[ts // 86400] += 1
        top = max(counts.values()) or 1
        return {
            "title": title, "color": color, "total": sum(counts.values()), "max": max(counts.values()),
            "bars": [
                {"label": datetime.fromtimestamp(d * 86400, tz=timezone.utc).strftime("%d/%m"),
                 "value": n, "height": max(int(100 * n / top), 3 if n else 0)}
                for d, n in counts.items()
            ],
        }

    @app.route("/g/<int:guild_id>/stats")
    @guild_required
    def stats_page(guild):
        start_day = int(time.time()) // 86400 - 29
        since = start_day * 86400

        def times(sql, *params):
            return [r["created_at"] for r in db.rows(sql, (guild.id, *params))]

        joins = times("SELECT created_at FROM member_events WHERE guild_id=? AND kind='join' AND created_at>=?", since)
        leaves = times("SELECT created_at FROM member_events WHERE guild_id=? AND kind='leave' AND created_at>=?", since)
        tickets = times("SELECT created_at FROM tickets WHERE guild_id=? AND created_at>=?", since)
        paid = int(db.one("SELECT COALESCE(SUM(price_cents), 0) AS s FROM tickets WHERE guild_id=? AND paid=1", (guild.id,))["s"] or 0)
        pending = int(db.one(
            "SELECT COALESCE(SUM(price_cents), 0) AS s FROM tickets WHERE guild_id=? AND quote_status='accepted' AND paid=0 AND closed_at IS NULL",
            (guild.id,),
        )["s"] or 0)
        return render_template(
            "stats.html", guild=guild, section="stats", paid=paid, pending=pending,
            charts=[day_chart("Arrivées", joins, start_day, "green"), day_chart("Départs", leaves, start_day, "red"),
                    day_chart("Tickets ouverts", tickets, start_day, "accent")],
            net=len(joins) - len(leaves),
        )

    # ---------- Journal des actions ----------
    @app.route("/g/<int:guild_id>/journal")
    @guild_required
    def journal(guild):
        entries = db.rows("SELECT * FROM audit_log WHERE guild_id=? ORDER BY id DESC LIMIT 200", (guild.id,))
        return render_template("journal.html", guild=guild, section="journal", entries=entries)

    # ---------- Annonces ----------
    @app.route("/g/<int:guild_id>/announce", methods=["GET", "POST"])
    @guild_required
    def announce(guild):
        if request.method == "POST":
            channel_raw = request.form.get("channel", "").strip()
            title = request.form.get("title", "").strip()
            text = request.form.get("text", "").strip()
            color = request.form.get("color", "info")
            channel = guild.get_channel(int(channel_raw)) if channel_raw.isdigit() else None
            if not isinstance(channel, discord.TextChannel):
                flash("Choisis le salon de l'annonce.", "error")
            elif not text or len(text) > 4000 or len(title) > 256:
                flash("Le texte est obligatoire (4000 caractères max, titre 256).", "error")
            elif color not in ("info", "success", "warn", "error", "mod"):
                flash("Couleur invalide.", "error")
            else:
                try:
                    link = run_in_bot(tools_cog().send_announcement(guild.id, channel.id, title, text, color))
                except discord.Forbidden:
                    flash("Le bot n'a pas la permission d'écrire dans ce salon.", "error")
                except Exception:
                    logging.exception("Annonce impossible")
                    flash("Impossible d'envoyer l'annonce pour le moment.", "error")
                else:
                    flash(f"Annonce envoyée ✅ {link}", "success")
                    log_action(guild, "Annonce envoyée", f"#{channel.name} : {title or text[:60]}")
            return redirect(url_for("announce", guild_id=guild.id))
        return render_template("announce.html", guild=guild, section="announce", options=select_options(guild))

    # ---------- Transcriptions des tickets ----------
    @app.route("/g/<int:guild_id>/transcripts")
    @guild_required
    def transcripts(guild):
        items = []
        for r in db.rows(
            "SELECT id, ticket_number, channel_name, user_id, closed_by, created_at FROM transcripts "
            "WHERE guild_id=? ORDER BY id DESC LIMIT 100", (guild.id,),
        ):
            member = guild.get_member(r["user_id"]) if r["user_id"] else None
            items.append({**dict(r), "client": member.display_name if member else str(r["user_id"] or "?")})
        return render_template("transcripts.html", guild=guild, section="transcripts", items=items)

    @app.route("/g/<int:guild_id>/transcripts/<int:transcript_id>")
    @guild_required
    def transcript_view(guild, transcript_id):
        row = db.one("SELECT * FROM transcripts WHERE id=? AND guild_id=?", (transcript_id, guild.id))
        if row is None:
            abort(404)
        return render_template("transcript_view.html", guild=guild, section="transcripts", t=row)

    # ---------- Partenaires : réglages + récompense ----------
    @app.route("/g/<int:guild_id>/partners/settings", methods=["POST"])
    @guild_required
    def partner_settings(guild):
        days = request.form.get("min_age", "0").strip()
        channel_raw = request.form.get("board_channel", "").strip()
        if not (days.isdigit() and 0 <= int(days) <= 365):
            flash("L'âge minimum doit être compris entre 0 et 365 jours.", "error")
        elif channel_raw and not (channel_raw.isdigit() and isinstance(guild.get_channel(int(channel_raw)), discord.TextChannel)):
            flash("Salon du classement introuvable.", "error")
        else:
            db.set_config(guild.id, "partner_min_age_days", int(days))
            if channel_raw:
                db.set_config(guild.id, "partner_board_channel", channel_raw)
            else:
                db.delete_config(guild.id, "partner_board_channel")
            flash("Réglages des partenaires enregistrés ✅", "success")
            log_action(guild, "Réglages partenaires modifiés", f"âge minimum : {days} j")
        return redirect(url_for("partners_admin", guild_id=guild.id))

    @app.route("/p/<int:guild_id>/claim", methods=["POST"])
    def partner_claim(guild_id):
        if "user" not in session:
            abort(400)
        guild = bot.get_guild(guild_id)
        uid = current_user_id()
        member = guild.get_member(uid) if guild else None
        partner = (
            db.one("SELECT * FROM partners WHERE guild_id=? AND user_id=? AND active=1", (guild_id, uid)) if member else None
        )
        if partner is None:
            abort(404)
        try:
            channel_id, created = run_in_bot(partners_cog().create_reward_ticket(guild_id, partner["id"]))
        except ValueError as error:
            flash(str(error), "error")
        except Exception:
            logging.exception("Ticket de récompense impossible")
            flash("Impossible d'ouvrir le ticket pour le moment. Réessaie dans un instant.", "error")
        else:
            channel = guild.get_channel(channel_id)
            name = f"#{channel.name}" if channel else "dans Discord"
            flash(
                f"Ticket de récompense ouvert ({name}) : l'équipe te répond sur Discord ✅" if created
                else f"Tu as déjà un ticket ouvert ({name}) : l'équipe te répond sur Discord.",
                "success",
            )
        return redirect(url_for("partner_space", guild_id=guild_id))

    # ---------- Tebex : notification d'achat ----------
    @app.route("/webhooks/tebex", methods=["POST"])
    def tebex_webhook():
        raw = request.get_data()
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            abort(400)
        if not isinstance(payload, dict):
            abort(400)
        if payload.get("type") == "validation.webhook":  # test de l'adresse par Tebex : on répond avec l'identifiant reçu
            return jsonify(id=payload.get("id"))
        if not tebex.verify_signature(raw, os.getenv("TEBEX_WEBHOOK_SECRET", ""), request.headers.get("X-Signature", "")):
            abort(401)
        if payload.get("type") == "payment.completed":
            try:
                run_in_bot(tools_cog().handle_purchase(tebex.summarize(payload), tebex.find_discord_id(payload)))
            except Exception:
                logging.exception("Traitement de l'achat Tebex impossible")
        return jsonify(id=payload.get("id"))

    return app
