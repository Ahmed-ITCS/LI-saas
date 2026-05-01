"""
Symmetric encryption for LinkedIn credentials using cryptography.Fernet.

If FERNET_KEY is not set in .env we auto-generate one at startup and warn loudly.
In production always set FERNET_KEY explicitly so data survives restarts.
"""
import os
import logging
from cryptography.fernet import Fernet

log = logging.getLogger(__name__)

_raw = os.getenv("FERNET_KEY", "").strip()
if _raw:
    _key = _raw.encode()
else:
    _key = Fernet.generate_key()
    log.warning(
        "⚠️  FERNET_KEY not set — generated a one-time key. "
        "Encrypted data will NOT survive restarts. Set FERNET_KEY in .env."
    )

_fernet = Fernet(_key)


def encrypt(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet.decrypt(token.encode()).decode()
