"""Statuts d'une commande client."""

STATUSES = {
    "attente": ("⏳", "En attente"),
    "cours": ("🔧", "En cours"),
    "terminee": ("✅", "Terminée"),
    "livree": ("📦", "Livrée"),
}


def label(key: str) -> str:
    emoji, name = STATUSES.get(key, STATUSES["attente"])
    return f"{emoji} {name}"
