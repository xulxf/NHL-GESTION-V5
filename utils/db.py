"""Accès à la base de données.

- Sans configuration : SQLite (fichier local, effacé à chaque déploiement sur l'offre gratuite de Render).
- Avec la variable DATABASE_URL : PostgreSQL externe (ex. Neon) -> les données sont conservées pour toujours.
"""
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from contextlib import closing
from pathlib import Path

CACHE_TTL = 30  # secondes : les réglages lus souvent sont gardés en mémoire
ID_TABLES = {
    "warns", "tickets", "reviews", "faq", "giveaways", "suggestions", "resources",
    "partners", "partner_referrals", "partner_tiers",
    "ticket_notes", "transcripts", "audit_log", "member_events", "applications",
}  # tables avec une colonne id


def use_postgres() -> bool:
    return bool(os.getenv("DATABASE_URL", "").strip())


# ---------- SQLite ----------
def _sqlite_connect():
    # DB_PATH permet de placer la base sur un disque persistant (ex. /var/data/bot.db)
    path = Path(os.getenv("DB_PATH", "data/bot.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _sqlite_exec(sql, params, mode):
    with closing(_sqlite_connect()) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        if mode == "one":
            return cur.fetchone()
        if mode == "all":
            return cur.fetchall()
        return cur.lastrowid, cur.rowcount


# ---------- PostgreSQL ----------
_pg_lock = threading.RLock()
_pg_conn = None


def _pg_connect():
    import pg8000.dbapi  # pur Python : s'installe partout

    # Certaines combinaisons de versions pg8000 / scramp n'exposent pas make_channel_binding :
    # sans cette fonction la connexion plante. On se rabat alors sur l'authentification SCRAM classique.
    try:
        import scramp

        if not hasattr(scramp, "make_channel_binding"):
            scramp.make_channel_binding = lambda *args, **kwargs: None
    except ImportError:
        pass

    url = urllib.parse.urlparse(os.environ["DATABASE_URL"].strip())
    query = urllib.parse.parse_qs(url.query)
    use_ssl = query.get("sslmode", ["require"])[0] != "disable"
    return pg8000.dbapi.connect(
        user=urllib.parse.unquote(url.username or ""),
        password=urllib.parse.unquote(url.password or ""),
        host=url.hostname,
        port=url.port or 5432,
        database=(url.path or "/").lstrip("/"),
        ssl_context=True if use_ssl else None,
        timeout=20,
    )


def _pg_rows(cur, rows):
    names = [c[0].decode() if isinstance(c[0], bytes) else c[0] for c in cur.description]
    return [dict(zip(names, row)) for row in rows]


def _pg_exec(sql, params, mode):
    """Une seule connexion partagée, recréée automatiquement si elle a été coupée."""
    global _pg_conn
    sql = sql.replace("?", "%s")
    insert = re.match(r"\s*INSERT\s+INTO\s+(\w+)", sql, re.I)
    returning = bool(insert) and insert.group(1).lower() in ID_TABLES and "RETURNING" not in sql.upper()
    if returning:
        sql += " RETURNING id"

    with _pg_lock:
        for attempt in (1, 2):
            try:
                if _pg_conn is None:
                    _pg_conn = _pg_connect()
                cur = _pg_conn.cursor()
                try:
                    cur.execute(sql, tuple(params))
                    if mode == "one":
                        fetched = cur.fetchone()
                        result = _pg_rows(cur, [fetched])[0] if fetched else None
                    elif mode == "all":
                        result = _pg_rows(cur, cur.fetchall())
                    else:
                        last_id = cur.fetchone()[0] if returning else None
                        result = (last_id, cur.rowcount)
                finally:
                    cur.close()
                _pg_conn.commit()
                return result
            except Exception:
                try:
                    _pg_conn.rollback()
                except Exception:
                    pass
                try:
                    _pg_conn.close()
                except Exception:
                    pass
                _pg_conn = None
                if attempt == 2:
                    raise


def _pg_statements():
    """Le schéma SQLite traduit pour PostgreSQL (les identifiants Discord ont besoin de BIGINT)."""
    text = SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    text = re.sub(r"\bINTEGER\b", "BIGINT", text)
    text = re.sub(r"\bBLOB\b", "BYTEA", text)
    return [stmt.strip() for stmt in text.split(";") if stmt.strip()]


# ---------- Interface commune ----------
def run(sql, params=()):
    """Écriture. Retourne (dernier id, nombre de lignes touchées)."""
    if use_postgres():
        return _pg_exec(sql, params, "write")
    return _sqlite_exec(sql, params, "write")


def one(sql, params=()):
    return _pg_exec(sql, params, "one") if use_postgres() else _sqlite_exec(sql, params, "one")


def rows(sql, params=()):
    return _pg_exec(sql, params, "all") if use_postgres() else _sqlite_exec(sql, params, "all")


SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    guild_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (guild_id, key)
);
CREATE TABLE IF NOT EXISTS warns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    mod_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'attente',
    message_id INTEGER,
    claimed_by INTEGER,
    created_at INTEGER NOT NULL,
    first_response_at INTEGER,
    closed_at INTEGER,
    reminded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    ticket_number INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    rating INTEGER NOT NULL,
    comment TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (guild_id, ticket_number)
);
CREATE TABLE IF NOT EXISTS faq (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS giveaways (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER,
    prize TEXT NOT NULL,
    winners INTEGER NOT NULL DEFAULT 1,
    ends_at INTEGER NOT NULL,
    ended INTEGER NOT NULL DEFAULT 0,
    host_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS giveaway_entries (
    giveaway_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (giveaway_id, user_id)
);
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER,
    user_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'attente',
    response TEXT
);
CREATE TABLE IF NOT EXISTS levels (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    xp INTEGER NOT NULL DEFAULT 0,
    level INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    url TEXT NOT NULL,
    category TEXT,
    role_id INTEGER,
    created_by INTEGER,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS resource_files (
    resource_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    data BLOB NOT NULL,
    PRIMARY KEY (resource_id, chunk_index)
);
CREATE TABLE IF NOT EXISTS partners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    invite_code TEXT NOT NULL,
    channel_id INTEGER,
    tier_id INTEGER,
    best_tier_min INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS partner_referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    partner_id INTEGER NOT NULL,
    member_id INTEGER NOT NULL,
    joined_at INTEGER NOT NULL,
    left_at INTEGER,
    UNIQUE (guild_id, member_id)
);
CREATE TABLE IF NOT EXISTS partner_tiers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    min_members INTEGER NOT NULL,
    reward TEXT,
    role_id INTEGER
);
CREATE TABLE IF NOT EXISTS ticket_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    ticket_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    ticket_number INTEGER NOT NULL,
    channel_name TEXT NOT NULL,
    user_id INTEGER,
    closed_by TEXT,
    content TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER,
    user_name TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS member_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS birthdays (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    day INTEGER NOT NULL,
    month INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS starboard (
    guild_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, message_id)
);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    answers TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'attente',
    message_id INTEGER,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_guild ON member_events (guild_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_guild ON audit_log (guild_id);
CREATE INDEX IF NOT EXISTS idx_transcripts_guild ON transcripts (guild_id);
CREATE INDEX IF NOT EXISTS idx_partners_guild ON partners (guild_id);
CREATE INDEX IF NOT EXISTS idx_referrals_partner ON partner_referrals (partner_id);
CREATE INDEX IF NOT EXISTS idx_resources_guild ON resources (guild_id);
CREATE INDEX IF NOT EXISTS idx_tickets_channel ON tickets (channel_id);
CREATE INDEX IF NOT EXISTS idx_tickets_guild ON tickets (guild_id);
CREATE TABLE IF NOT EXISTS level_roles (
    guild_id INTEGER NOT NULL,
    level INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, level)
);
"""


# Colonnes ajoutées après coup à des tables existantes (migration automatique)
MIGRATIONS = [
    ("resources", "file_name", "TEXT"),
    ("resources", "file_type", "TEXT"),
    ("resources", "file_size", "INTEGER"),
    ("tickets", "price_cents", "INTEGER"),
    ("tickets", "quote_status", "TEXT"),
    ("tickets", "paid", "INTEGER DEFAULT 0"),
    ("tickets", "priority", "TEXT DEFAULT 'normal'"),
    ("tickets", "staff_reminded", "INTEGER DEFAULT 0"),
]


def _migrate():
    if use_postgres():
        for table, column, kind in MIGRATIONS:
            kind = kind.replace("INTEGER", "BIGINT")
            _pg_exec(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind}", (), "write")
        return
    with closing(_sqlite_connect()) as conn:
        for table, column, kind in MIGRATIONS:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        conn.commit()


def init():
    if use_postgres():
        for statement in _pg_statements():
            _pg_exec(statement, (), "write")
        _migrate()
        logging.info("Base de données : PostgreSQL externe (données conservées)")
    else:
        with closing(_sqlite_connect()) as conn:
            conn.executescript(SCHEMA)
            conn.commit()
        _migrate()
        logging.info("Base de données : SQLite locale (effacée à chaque déploiement sur Render gratuit)")


# ---------- Configuration par serveur ----------
_cache = {}  # (guild_id, clé) -> (valeur, expiration)


def get_config(guild_id, key, default=None):
    now = time.monotonic()
    hit = _cache.get((guild_id, key))
    if hit and hit[1] > now:
        value = hit[0]
    else:
        row = one("SELECT value FROM config WHERE guild_id=? AND key=?", (guild_id, key))
        value = row["value"] if row else None
        _cache[(guild_id, key)] = (value, now + CACHE_TTL)
    return value if value is not None else default


def get_int(guild_id, key):
    value = get_config(guild_id, key)
    return int(value) if value else None


def set_config(guild_id, key, value):
    run(
        "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
        (guild_id, key, str(value)),
    )
    _cache[(guild_id, key)] = (str(value), time.monotonic() + CACHE_TTL)


def delete_config(guild_id, key):
    run("DELETE FROM config WHERE guild_id=? AND key=?", (guild_id, key))
    _cache[(guild_id, key)] = (None, time.monotonic() + CACHE_TTL)


# ---------- Avertissements ----------
def add_warn(guild_id, user_id, mod_id, reason) -> int:
    last_id, _ = run(
        "INSERT INTO warns (guild_id, user_id, mod_id, reason, created_at) VALUES (?, ?, ?, ?, ?)",
        (guild_id, user_id, mod_id, reason, int(time.time())),
    )
    return last_id


def get_warns(guild_id, user_id):
    return rows("SELECT * FROM warns WHERE guild_id=? AND user_id=? ORDER BY id", (guild_id, user_id))


def delete_warn(guild_id, warn_id) -> bool:
    _, count = run("DELETE FROM warns WHERE id=? AND guild_id=?", (warn_id, guild_id))
    return count > 0


def clear_warns(guild_id, user_id) -> int:
    _, count = run("DELETE FROM warns WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    return count
