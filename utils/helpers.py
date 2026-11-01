"""Petites fonctions utilitaires sans dépendance à Discord."""
import re

_DURATION_RE = re.compile(r"(\d+)([smhdj])")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "j": 86400}


def parse_duration(text: str):
    """'1h30m', '2j', '45m' -> secondes (None si invalide)."""
    cleaned = (text or "").replace(" ", "").lower()
    if not cleaned:
        return None
    pos, total = 0, 0
    for match in _DURATION_RE.finditer(cleaned):
        if match.start() != pos:
            return None
        total += int(match.group(1)) * _UNITS[match.group(2)]
        pos = match.end()
    return total if pos == len(cleaned) and total > 0 else None


def fmt_duration(seconds) -> str:
    seconds = int(seconds)
    if seconds < 3600:
        return f"{max(seconds // 60, 1)} min"
    if seconds < 86400:
        return f"{seconds // 3600} h {seconds % 3600 // 60:02d}"
    return f"{seconds // 86400} j {seconds % 86400 // 3600} h"


def xp_needed(level: int) -> int:
    """XP nécessaire pour passer du niveau `level` au suivant."""
    return 5 * level * level + 50 * level + 100


def level_progress(total_xp: int):
    """Retourne (niveau, xp dans le niveau, xp requis pour le niveau suivant)."""
    level, xp = 0, total_xp
    while xp >= xp_needed(level):
        xp -= xp_needed(level)
        level += 1
    return level, xp, xp_needed(level)


def stars(n: int) -> str:
    return "⭐" * n + "☆" * (5 - n)


def used_invite(old: dict, new: dict):
    """Code de l'invitation dont le nombre d'utilisations a augmenté (None si aucune)."""
    for code, uses in new.items():
        if uses > old.get(code, 0):
            return code
    return None


def tier_for(tiers, count: int):
    """Palier le plus élevé atteint avec `count` membres (None si aucun)."""
    current = None
    for tier in sorted(tiers, key=lambda t: t["min_members"]):
        if count >= tier["min_members"]:
            current = tier
    return current


def tier_progress(tiers, count: int):
    """Retourne (palier actuel, prochain palier, pourcentage vers le prochain)."""
    ordered = sorted(tiers, key=lambda t: t["min_members"])
    current = tier_for(ordered, count)
    upcoming = next((t for t in ordered if t["min_members"] > count), None)
    if upcoming is None:
        return current, None, 100
    base = current["min_members"] if current else 0
    return current, upcoming, int(100 * (count - base) / (upcoming["min_members"] - base))
