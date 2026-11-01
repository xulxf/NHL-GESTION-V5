"""Personnalisation : nom et logos, lus depuis le fichier .env.

Les images Discord ont besoin d'une adresse (URL) publique et permanente.
"""
import os


def _env(key):
    value = os.getenv(key, "").strip()
    return value or None


def name() -> str:
    """Nom affiché dans le pied de page des embeds (BRAND_NAME)."""
    return os.getenv("BRAND_NAME", "").strip()


def logo(section=None):
    """Logo d'une section (ex. WELCOME_LOGO_URL), sinon le logo par défaut (LOGO_URL)."""
    if section:
        specific = _env(f"{section.upper()}_LOGO_URL")
        if specific:
            return specific
    return _env("LOGO_URL")


def image(section):
    """Grande image / bannière d'une section (ex. WELCOME_IMAGE_URL)."""
    return _env(f"{section.upper()}_IMAGE_URL")
