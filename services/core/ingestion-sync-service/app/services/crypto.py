"""Cifratura simmetrica dei segreti a riposo (token per-repository).

Usa Fernet (AES-128 in CBC + HMAC) con una chiave derivata via SHA-256 dalla
passphrase in ``settings.settings_enc_key``: così la variabile d'ambiente può
essere una stringa qualsiasi. In assenza di chiave dedicata si ripiega su una
di sviluppo (con warning) per non bloccare la piattaforma in locale.
"""
import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

logger = logging.getLogger(__name__)

_DEV_FALLBACK = "sdlc-dev-encryption-key-change-me"


def _fernet() -> Fernet:
    passphrase = settings.settings_enc_key or _DEV_FALLBACK
    if not settings.settings_enc_key:
        logger.warning(
            "SETTINGS_ENC_KEY non impostata: uso una chiave di sviluppo. "
            "In produzione imposta una chiave dedicata."
        )
    key = base64.urlsafe_b64encode(hashlib.sha256(passphrase.encode("utf-8")).digest())
    return Fernet(key)


def encrypt(plaintext: str) -> bytes:
    """Cifra una stringa; ritorna il token Fernet in bytes (da salvare in DB)."""
    return _fernet().encrypt(plaintext.encode("utf-8"))


def decrypt(token: bytes | None) -> str | None:
    """Decifra un token Fernet. Ritorna None se assente o non decifrabile
    (es. chiave cambiata): il chiamante ripiegherà sul token globale."""
    if not token:
        return None
    try:
        return _fernet().decrypt(bytes(token)).decode("utf-8")
    except (InvalidToken, ValueError):
        logger.warning("Token cifrato non decifrabile (chiave cambiata?)")
        return None
