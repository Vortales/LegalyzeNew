"""
secure_store.py — локальное (машинно-привязанное) шифрование секретов,
безопасное эфемерное рабочее пространство для временных файлов и
надёжное удаление (shred) артефактов.

Назначение (п.8 ТЗ):
  * пароль в config.json больше НИКОГДА не хранится в открытом виде;
  * расшифрованные данные промта не остаются на диске в открытом виде
    (никаких client/data/_temp_prompt.pdf);
  * готовый PDF кэшируется на диск ТОЛЬКО в зашифрованном виде (п.9),
    а в открытом виде существует лишь в эфемерной temp-папке сессии,
    которая затирается при выходе.

Модуль не требует обязательных зависимостей: если установлен
`cryptography`, используется Fernet(AES-128-CBC+HMAC), иначе — встроенный
AES-независимый поток HMAC-SHA256-CTR + HMAC-SHA256 тег (совместимый формат
с префиксом версии).
"""

from __future__ import annotations
import diagnostics as diag
from diagnostics import stage

import atexit
import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import struct
import tempfile
from pathlib import Path
from typing import Optional

try:  # опционально
    from cryptography.fernet import Fernet, InvalidToken  # type: ignore
except Exception:  # pragma: no cover
    diag.exception("secure_store.py:40")
    Fernet = None  # type: ignore
    InvalidToken = Exception  # type: ignore

try:
    from hwid_gen import generate_hwid
except Exception:  # pragma: no cover
    diag.exception("secure_store.py:47")
    @stage
    def generate_hwid() -> str:
        return ""


_APP_SALT = b"Legalyze::local-secret-store::v1"
_PREFIX_FERNET = "lgz1:"
_PREFIX_FALLBACK = "lgz2:"


# --------------------------------------------------------------------------- #
#  Ключ, привязанный к машине
# --------------------------------------------------------------------------- #
def _machine_seed() -> bytes:
    parts = [
        generate_hwid() or "",
        os.environ.get("COMPUTERNAME", ""),
        os.environ.get("USERNAME", ""),
        os.environ.get("PROCESSOR_IDENTIFIER", ""),
    ]
    return ("|".join(parts)).encode("utf-8", errors="ignore")


def _master_key() -> bytes:
    return hashlib.pbkdf2_hmac("sha256", _machine_seed(), _APP_SALT, 120_000, dklen=32)


def _fernet():
    if Fernet is None:
        return None
    try:
        return Fernet(base64.urlsafe_b64encode(_master_key()))
    except Exception:
        diag.exception("secure_store.py:81")
        return None


# --------------------------------------------------------------------------- #
#  Резервный поточный шифр (HMAC-SHA256-CTR + HMAC тег)
# --------------------------------------------------------------------------- #
def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + struct.pack(">I", counter), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def _fallback_encrypt(data: bytes) -> bytes:
    key = _master_key()
    enc_key = hashlib.sha256(b"enc" + key).digest()
    mac_key = hashlib.sha256(b"mac" + key).digest()
    nonce = secrets.token_bytes(16)
    stream = _keystream(enc_key, nonce, len(data))
    body = bytes(a ^ b for a, b in zip(data, stream))
    tag = hmac.new(mac_key, nonce + body, hashlib.sha256).digest()
    return nonce + tag + body


def _fallback_decrypt(blob: bytes) -> bytes:
    key = _master_key()
    enc_key = hashlib.sha256(b"enc" + key).digest()
    mac_key = hashlib.sha256(b"mac" + key).digest()
    nonce, tag, body = blob[:16], blob[16:48], blob[48:]
    if not hmac.compare_digest(tag, hmac.new(mac_key, nonce + body, hashlib.sha256).digest()):
        raise ValueError("secure_store: bad MAC")
    stream = _keystream(enc_key, nonce, len(body))
    return bytes(a ^ b for a, b in zip(body, stream))


# --------------------------------------------------------------------------- #
#  Публичное API: строки (пароль в конфиге)
# --------------------------------------------------------------------------- #
def encrypt_secret(plain: str) -> str:
    """Возвращает строку вида 'lgz1:<base64>' — безопасна для config.json."""
    if plain is None:
        return ""
    if plain == "":
        return ""
    if is_encrypted(plain):
        return plain

    raw = plain.encode("utf-8")
    f = _fernet()
    if f is not None:
        return _PREFIX_FERNET + f.encrypt(raw).decode("ascii")
    return _PREFIX_FALLBACK + base64.urlsafe_b64encode(_fallback_encrypt(raw)).decode("ascii")


def decrypt_secret(value: str) -> str:
    """Расшифровывает строку, полученную из encrypt_secret. Пустая строка -> ''. """
    if not value:
        return ""
    try:
        if value.startswith(_PREFIX_FERNET):
            f = _fernet()
            if f is None:
                return ""
            return f.decrypt(value[len(_PREFIX_FERNET):].encode("ascii")).decode("utf-8")
        if value.startswith(_PREFIX_FALLBACK):
            blob = base64.urlsafe_b64decode(value[len(_PREFIX_FALLBACK):].encode("ascii"))
            return _fallback_decrypt(blob).decode("utf-8")
    except Exception:
        diag.exception("secure_store.py:152")
        return ""
    # legacy: строка была сохранена в открытом виде
    return value


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and (
        value.startswith(_PREFIX_FERNET) or value.startswith(_PREFIX_FALLBACK)
    )


# --------------------------------------------------------------------------- #
#  Публичное API: бинарные блоки (кэш готового PDF, п.9)
# --------------------------------------------------------------------------- #
def encrypt_blob(data: bytes) -> bytes:
    f = _fernet()
    if f is not None:
        return b"F1" + f.encrypt(data)
    return b"X1" + _fallback_encrypt(data)


def decrypt_blob(blob: bytes) -> bytes:
    if blob[:2] == b"F1":
        f = _fernet()
        if f is None:
            raise ValueError("secure_store: fernet unavailable")
        return f.decrypt(blob[2:])
    if blob[:2] == b"X1":
        return _fallback_decrypt(blob[2:])
    raise ValueError("secure_store: unknown blob format")


# --------------------------------------------------------------------------- #
#  Надёжное удаление + эфемерная рабочая папка
# --------------------------------------------------------------------------- #
def shred_file(path) -> None:
    """Перезаписывает файл случайными данными и удаляет его."""
    try:
        p = Path(path)
        if not p.is_file():
            return
        size = p.stat().st_size
        with open(p, "r+b", buffering=0) as fh:
            for _ in range(2):
                fh.seek(0)
                fh.write(secrets.token_bytes(size))
                fh.flush()
                os.fsync(fh.fileno())
        p.unlink(missing_ok=True)
    except Exception:
        diag.exception("secure_store.py:203")
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            diag.exception("secure_store.py:207")
            pass


def purge_plaintext_artifacts(data_dir) -> None:
    """Удаляет любые открытые артефакты, оставшиеся от старых версий клиента."""
    try:
        d = Path(data_dir)
        if not d.exists():
            return
        for pattern in ("_temp_prompt.pdf", "*.pdf", "*.txt.dec", "_prompt*.pdf"):
            for f in d.glob(pattern):
                shred_file(f)
    except Exception:
        diag.exception("secure_store.py:221")
        pass


class SecureWorkspace:
    """
    Эфемерная папка сессии для файлов, которые физически обязаны существовать
    на диске (Chrome умеет загружать только реальный путь).
    Уничтожается при выходе процесса (atexit) и по вызову dispose().
    """

    def __init__(self, prefix: str = "lgz_"):
        self._dir = Path(tempfile.mkdtemp(prefix=prefix))
        try:
            os.chmod(self._dir, 0o700)
        except Exception:
            diag.exception("secure_store.py:237")
            pass
        atexit.register(self.dispose)

    @property
    def path(self) -> Path:
        return self._dir

    def file(self, name: str) -> Path:
        return self._dir / name

    def write(self, name: str, data: bytes) -> Path:
        target = self.file(name)
        # старый файл с тем же именем затираем, а не просто перезаписываем
        if target.exists():
            shred_file(target)
        target.write_bytes(data)
        try:
            os.chmod(target, 0o600)
        except Exception:
            diag.exception("secure_store.py:257")
            pass
        return target

    def clear(self) -> None:
        try:
            for f in self._dir.glob("*"):
                if f.is_file():
                    shred_file(f)
        except Exception:
            diag.exception("secure_store.py:267")
            pass

    def dispose(self) -> None:
        self.clear()
        try:
            shutil.rmtree(self._dir, ignore_errors=True)
        except Exception:
            diag.exception("secure_store.py:275")
            pass


# --------------------------------------------------------------------------- #
#  Манифест кэша промтов (п.9)
# --------------------------------------------------------------------------- #
class PromptCacheManifest:
    """
    Хранит для каждого промта: контрольную сумму серверного файла,
    хэш шаблона и хэш готового PDF. Позволяет пропустить скачивание,
    расшифровку и рендер, если на сервере ничего не менялось.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._data = {}
        self.load()

    def load(self) -> None:
        try:
            if self.path.exists():
                self._data = json.loads(self.path.read_text("utf-8"))
                if not isinstance(self._data, dict):
                    self._data = {}
        except Exception:
            diag.exception("secure_store.py:301")
            self._data = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), "utf-8")
        except Exception:
            diag.exception("secure_store.py:309")
            pass

    def get(self, key: str) -> dict:
        value = self._data.get(key)
        return value if isinstance(value, dict) else {}

    def put(self, key: str, **fields) -> None:
        entry = self.get(key)
        entry.update(fields)
        self._data[key] = entry
        self.save()

    def drop(self, key: str) -> None:
        if key in self._data:
            self._data.pop(key, None)
            self.save()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def template_hash(template: Optional[dict]) -> str:
    try:
        payload = json.dumps(template or {}, sort_keys=True, ensure_ascii=False)
    except Exception:
        diag.exception("secure_store.py:336")
        payload = str(template)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
