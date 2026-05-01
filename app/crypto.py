import hashlib, base64
from cryptography.fernet import Fernet

def _get_fernet(secret_key: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest()))

def encrypt_token(token: str, secret_key: str) -> str:
    return _get_fernet(secret_key).encrypt(token.encode()).decode()

def decrypt_token(encrypted: str, secret_key: str) -> str:
    return _get_fernet(secret_key).decrypt(encrypted.encode()).decode()

def token_to_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]
