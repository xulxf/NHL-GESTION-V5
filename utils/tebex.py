"""Outils pour les notifications (webhooks) de la boutique Tebex."""
import hashlib
import hmac
import re

DISCORD_ID_RE = re.compile(r"^\d{17,20}$")


def verify_signature(raw_body: bytes, secret: str, signature: str) -> bool:
    """Signature Tebex : HMAC-SHA256( sha256(corps), secret ), en hexadécimal."""
    if not secret or not signature:
        return False
    body_hash = hashlib.sha256(raw_body).hexdigest()
    expected = hmac.new(secret.encode(), body_hash.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def find_discord_id(node, hint=False):
    """Cherche un identifiant Discord (17 à 20 chiffres) dans la notification, notamment dans les
    variables de produit ou les champs dont le nom contient « discord »."""
    if isinstance(node, dict):
        # variables de produit : {"identifier": "discord_id", "option": "123..."}
        label = " ".join(str(node.get(k, "")) for k in ("identifier", "name", "variable")).lower()
        if "discord" in label:
            for key in ("option", "value", "answer"):
                value = str(node.get(key, "")).strip()
                if DISCORD_ID_RE.match(value):
                    return int(value)
        for key, value in node.items():
            found = find_discord_id(value, hint=hint or "discord" in str(key).lower())
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = find_discord_id(item, hint)
            if found:
                return found
    elif hint and isinstance(node, (str, int)) and DISCORD_ID_RE.match(str(node).strip()):
        return int(str(node).strip())
    return None


def summarize(payload: dict) -> dict:
    """Résumé lisible d'un paiement : acheteur, produits, montant."""
    subject = payload.get("subject") or {}
    customer = subject.get("customer") or {}
    username = customer.get("username") or {}
    products = [p.get("name", "Produit") for p in (subject.get("products") or []) if isinstance(p, dict)]
    price = subject.get("price") or {}
    amount = price.get("amount")
    return {
        "transaction": str(subject.get("transaction_id") or ""),
        "buyer": (username.get("username") if isinstance(username, dict) else None) or customer.get("first_name") or "inconnu",
        "products": products or ["Produit"],
        "amount": f"{amount} {price.get('currency', '')}".strip() if amount is not None else "",
    }
