import diagnostics as diag
from diagnostics import stage
import json
import os
import shutil
import sys
from pathlib import Path


def _is_compiled() -> bool:
    """
    True, если приложение собрано в exe.
    """
    return getattr(sys, "frozen", False) or "__compiled__" in globals()


def _script_dir() -> Path:
    """
    Директория исходников при обычном запуске через python.
    """
    try:
        return Path(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        diag.exception("config.py:24")
        return Path.cwd()


def _bundle_dir() -> Path:
    """
    Директория, где лежат ресурсы приложения.

    Для Nuitka --onefile это обычно временная папка, куда распакованы ресурсы.
    Для обычного запуска это папка исходников.
    """
    if _is_compiled():
        try:
            return Path(os.path.dirname(os.path.abspath(sys.executable)))
        except Exception:
            diag.exception("config.py:39")
            return _script_dir()

    return _script_dir()


def _writable_app_dir() -> Path:
    from storage_paths import app_dir
    return app_dir()


def _legacy_local_dir():
    """
    Старое расположение данных рядом с exe/скриптом.

    Используется только для одноразовой миграции старого config.json,
    если он уже существовал рядом с приложением.
    """
    try:
        if _is_compiled():
            return Path(os.path.dirname(os.path.abspath(sys.executable)))

        return _script_dir()
    except Exception:
        diag.exception("config.py:73")
        return None


APP_DIR = _writable_app_dir()

DATA_DIR = APP_DIR / "data"
CONFIG_FILE = APP_DIR / "config.json"
PROMPTS_DIR = DATA_DIR / "prompts"

# ТЗ 1.1–1.2: Шаблон.txt лежит ВСЕГДА рядом с config.json, не шифруется.
# Имя файла статично и никогда не меняется.
TEMPLATE_FILE = APP_DIR / "Шаблон.txt"

# RESOURCE_DIR / BUNDLE_DATA_DIR нужны только для чтения встроенных ресурсов,
# например шрифта, который распаковывается во временную папку --onefile.
RESOURCE_DIR = _bundle_dir()
BUNDLE_DATA_DIR = RESOURCE_DIR / "data"
ENCRYPTION_KEY = b"legalyze-fernet-key-32bytes-long!"
SERVER_URL = "https://legalyzeai.ru"
CURRENT_VERSION = "1.0.5"

# Порядок полей в Шаблон.txt (фиксированный, менять нельзя).
TEMPLATE_FIELDS = ("Name", "ID", "Fraction", "Rang", "Department", "JobTitle")

# Шаблон по умолчанию (ТЗ п.3: Mike Macmillan, 85028, SANG, 11, MP, Заместитель командующего MP, сенатор NG).
DEFAULT_TEMPLATE = {
    "Name": "Mike Macmillan",
    "ID": "85028",
    "Fraction": "SANG",
    "Rang": "11",
    "Department": "MP",
    "JobTitle": "Заместитель командующего MP, сенатор NG",
}

DEFAULT_CONFIG = {
    "login": "",
    "password": "",
    "remember": False,
    "token": "",
    "selected_prompt": "",
    "hotkey_toggle": "F2",
    "hotkey_mic": "F3",
    "template": DEFAULT_TEMPLATE.copy(),
}


def _migrate_legacy_config() -> None:
    """
    Если рядом со старым exe/скриптом уже был config.json,
    копируем его в новое расположение в %APPDATA%.

    Это выполняется только один раз: если новый config.json уже есть,
    ничего не делаем.
    """
    try:
        if CONFIG_FILE.exists():
            return

        legacy_dir = _legacy_local_dir()

        if not legacy_dir:
            return

        legacy_config = legacy_dir / "config.json"

        if legacy_config.exists() and legacy_config.is_file():
            shutil.copy2(legacy_config, CONFIG_FILE)
    except Exception:
        diag.exception("config.py:142")
        pass


def _copy_bundle_font() -> None:
    """
    Копирует DejaVuSans.ttf из ресурсов приложения в постоянную DATA_DIR.

    Это важно для --onefile, потому что включённый через --include-data-file
    шрифт находится во временной папке, а PDF-генератор будет искать его в
    постоянной папке данных.
    """
    try:
        dst = DATA_DIR / "DejaVuSans.ttf"

        if dst.exists():
            return

        candidates = []

        # Если пользователь вдруг положил data рядом с оригинальным exe
        try:
            if _is_compiled() and sys.argv and sys.argv[0]:
                argv_dir = Path(sys.argv[0]).parent
                candidates.append(argv_dir / "data" / "DejaVuSans.ttf")
        except Exception:
            diag.exception("config.py:168")
            pass

        # Основной вариант для --onefile: ресурс рядом с распакованным бинарником
        candidates.append(BUNDLE_DATA_DIR / "DejaVuSans.ttf")

        # Вариант для обычного запуска из исходников
        candidates.append(_script_dir() / "data" / "DejaVuSans.ttf")

        for src in candidates:
            try:
                if not src.exists():
                    continue

                if not src.is_file():
                    continue

                if os.path.abspath(src) == os.path.abspath(dst):
                    continue

                shutil.copy2(src, dst)
                return
            except Exception:
                diag.exception("config.py:191")
                continue
    except Exception:
        diag.exception("config.py:194")
        pass


@stage
def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)

    _migrate_legacy_config()
    _copy_bundle_font()


@stage
def load_config() -> dict:
    ensure_dirs()

    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)

            for k, v in DEFAULT_CONFIG.items():
                if k not in cfg:
                    cfg[k] = v.copy() if isinstance(v, dict) else v

            cfg["template"] = normalize_template(cfg.get("template"))
            return cfg
        except Exception:
            diag.exception("config.py:223")
            pass

    cfg = DEFAULT_CONFIG.copy()
    cfg["template"] = DEFAULT_TEMPLATE.copy()

    try:
        save_config(cfg)
    except Exception:
        diag.exception("config.py:232")
        pass

    return cfg


def save_config(cfg: dict):
    ensure_dirs()

    tmp_file = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")

    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    os.replace(tmp_file, CONFIG_FILE)


def get_template_filled(cfg: dict) -> bool:
    t = cfg.get("template", {})
    return any(str(v).strip() for v in t.values())


# --------------------------------------------------------------------------- #
#  Шаблон.txt — открытый файл рядом с config.json (ТЗ 1.1, 1.2, 1.4)
# --------------------------------------------------------------------------- #

def normalize_template(template: dict | None) -> dict:
    """Приводит шаблон к каноническому виду: все 6 полей, значения — строки."""
    template = template or {}
    result = {}
    for field in TEMPLATE_FIELDS:
        val = template.get(field, DEFAULT_TEMPLATE.get(field, ""))
        result[field] = str(val if val is not None else "")
    return result


def read_template_file() -> dict:
    """Читает Шаблон.txt (JSON). Нет файла / битый — возвращает шаблон по умолчанию."""
    try:
        if TEMPLATE_FILE.exists():
            data = json.loads(TEMPLATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return normalize_template(data)
    except Exception:
        diag.exception("config.py:276")
        pass
    return normalize_template(DEFAULT_TEMPLATE)


def write_template_file(template: dict | None) -> Path:
    """Перезаписывает Шаблон.txt из словаря шаблона. Файл НЕ шифруется."""
    ensure_dirs()
    data = normalize_template(template)
    tmp_file = TEMPLATE_FILE.with_name(TEMPLATE_FILE.name + ".tmp")
    tmp_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp_file, TEMPLATE_FILE)
    return TEMPLATE_FILE


def ensure_template_file(template: dict | None = None) -> Path:
    """Создаёт Шаблон.txt с базовыми параметрами, если его нет."""
    ensure_dirs()
    if not TEMPLATE_FILE.exists():
        return write_template_file(template or DEFAULT_TEMPLATE)
    return TEMPLATE_FILE


def save_template(cfg: dict, template: dict | None) -> dict:
    """Единая точка сохранения шаблона: config.json + Шаблон.txt (ТЗ 1.2)."""
    data = normalize_template(template)
    cfg["template"] = data
    save_config(cfg)
    try:
        write_template_file(data)
    except Exception:
        diag.exception("config.py:309")
        pass
    return cfg
