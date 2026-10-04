import diagnostics as diag
from diagnostics import stage
import base64
import hashlib
import zlib

from cryptography.fernet import Fernet

from config import ENCRYPTION_KEY


def _derive_key(raw_key: bytes) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(raw_key).digest())


_fernet = Fernet(_derive_key(ENCRYPTION_KEY))

# Legacy-ключ старого encrypt_tool.py (оставлен только для чтения старых .enc).
_LEGACY_KEY_RAW = b"LegalyzeSecretKey"
_legacy_fernet = Fernet(_derive_key(_LEGACY_KEY_RAW))


def encrypt_data(data: bytes) -> bytes:
    """Новый единый формат: Fernet поверх сырых байтов (годится и для PDF)."""
    return _fernet.encrypt(data)


def decrypt_data(encrypted: bytes) -> bytes:
    """Расшифровка нового формата."""
    return _fernet.decrypt(encrypted)


def decrypt_data_auto(encrypted: bytes) -> bytes:
    """Расшифровка нового формата + legacy (старый ключ + zlib).

    Нужна на переходный период, пока на сервере могут лежать .enc,
    зашифрованные старой версией encrypt_tool.py.
    """
    try:
        return _fernet.decrypt(encrypted)
    except Exception:
        diag.exception("crypto_utils.py:42")
        pass
    # Старый формат: другой ключ, внутри zlib-сжатый текст.
    raw = _legacy_fernet.decrypt(encrypted)
    try:
        return zlib.decompress(raw)
    except Exception:
        diag.exception("crypto_utils.py:49")
        return raw


def encrypt_file(input_path: str, output_path: str):
    with open(input_path, "rb") as f:
        data = f.read()
    encrypted = encrypt_data(data)
    with open(output_path, "wb") as f:
        f.write(encrypted)


def decrypt_file_to_bytes(encrypted_path: str) -> bytes:
    with open(encrypted_path, "rb") as f:
        encrypted = f.read()
    return decrypt_data_auto(encrypted)
