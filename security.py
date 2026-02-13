"""
Security module for RepDoctor2.
Handles credential encryption/decryption using Fernet + PBKDF2.
"""

import base64
import json
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

CREDENTIALS_PATH = os.path.join(os.path.dirname(__file__), "config", "credentials.enc")
SALT_LENGTH = 16


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def encrypt_credentials(password: str, github_pat: str, anthropic_key: str) -> None:
    """Encrypt and store GitHub PAT and Anthropic API key."""
    os.makedirs(os.path.dirname(CREDENTIALS_PATH), exist_ok=True)
    salt = os.urandom(SALT_LENGTH)
    key = _derive_key(password, salt)
    fernet = Fernet(key)
    payload = json.dumps({
        "github_pat": github_pat,
        "anthropic_key": anthropic_key,
    }).encode()
    encrypted = fernet.encrypt(payload)
    with open(CREDENTIALS_PATH, "wb") as f:
        f.write(salt + encrypted)


def decrypt_credentials(password: str) -> dict | None:
    """Decrypt and return credentials. Returns None if wrong password or missing file."""
    if not os.path.exists(CREDENTIALS_PATH):
        return None
    with open(CREDENTIALS_PATH, "rb") as f:
        data = f.read()
    salt = data[:SALT_LENGTH]
    encrypted = data[SALT_LENGTH:]
    key = _derive_key(password, salt)
    fernet = Fernet(key)
    try:
        decrypted = fernet.decrypt(encrypted)
        return json.loads(decrypted.decode())
    except InvalidToken:
        return None


def credentials_exist() -> bool:
    return os.path.exists(CREDENTIALS_PATH)


def delete_credentials() -> None:
    if os.path.exists(CREDENTIALS_PATH):
        os.remove(CREDENTIALS_PATH)
