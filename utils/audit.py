"""Journal des actions faites depuis le dashboard."""
import time

from utils import db


def record(guild_id, user_id, user_name, action, detail=""):
    db.run(
        "INSERT INTO audit_log (guild_id, user_id, user_name, action, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (guild_id, int(user_id) if user_id else None, (user_name or "")[:80], action[:120], (detail or "")[:400], int(time.time())),
    )
