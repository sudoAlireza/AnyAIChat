import logging
import os
import threading
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_fernet = None
_fernet_lock = threading.Lock()


def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet

    with _fernet_lock:
        # Double-check after acquiring lock
        if _fernet is not None:
            return _fernet

        key = os.getenv("ENCRYPTION_KEY", "")
        if not key:
            logger.warning(
                "ENCRYPTION_KEY not set. Generating a temporary key. "
                "Set ENCRYPTION_KEY env var for persistent encryption."
            )
            key = Fernet.generate_key().decode()

        try:
            _fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:
            logger.error(
                "Invalid ENCRYPTION_KEY: %s. Generating temporary key. "
                "Previously encrypted data will NOT be decryptable!", exc
            )
            _fernet = Fernet(Fernet.generate_key())

    return _fernet


def encrypt_api_key(api_key: str) -> str:
    """Encrypt an API key for storage."""
    f = _get_fernet()
    return f.encrypt(api_key.encode()).decode()


def decrypt_api_key(encrypted_key: str) -> str:
    """Decrypt an API key from storage."""
    f = _get_fernet()
    try:
        return f.decrypt(encrypted_key.encode()).decode()
    except InvalidToken:
        # DEPRECATED: Key might be stored in plaintext (legacy data before encryption was added)
        logger.warning("Failed to decrypt API key - returning as-is (possibly plaintext legacy key)")
        return encrypted_key


def generate_encryption_key() -> str:
    """Generate a new Fernet encryption key. Useful for initial setup."""
    return Fernet.generate_key().decode()
