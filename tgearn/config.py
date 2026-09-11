"""Environment-only bot credentials; local .env parsing without execution."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re


class ConfigError(ValueError):
    pass


def load_dotenv(path):
    path = Path(path)
    if not path.exists():
        return
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ConfigError(f"В .env ошибка в строке {number}.")
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise ConfigError(f"В .env незакрытая кавычка в строке {number}.")
            value = value[1:-1]
        os.environ.setdefault(key, value)


def user_ids(value, label):
    if not value.strip():
        return frozenset()
    ids = set()
    for part in value.split(","):
        part = part.strip()
        if not part.isascii() or not part.isdigit() or not 0 < int(part) < 2**63:
            raise ConfigError(f"{label}: нужны числовые Telegram ID через запятую.")
        ids.add(int(part))
    return frozenset(ids)


@dataclass(frozen=True)
class BotConfig:
    kind: str
    token: str = field(repr=False)
    allowed_ids: frozenset[int]
    database: Path


def read_config(kind, root=None):
    if kind not in {"store", "buyback"}:
        raise ConfigError("Выберите store или buyback.")
    root = Path(root or Path.cwd()).resolve()
    load_dotenv(root / ".env")
    store_path = (root / os.environ.get("STORE_DB", ".data/store.sqlite3")).resolve()
    buyer_path = (root / os.environ.get("BUYBACK_DB", ".data/buyback.sqlite3")).resolve()
    if store_path == buyer_path:
        raise ConfigError("Для магазина и скупки нужны разные файлы базы.")
    prefix = "STORE" if kind == "store" else "BUYBACK"
    token = os.environ.get(prefix + "_BOT_TOKEN", "").strip()
    if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{30,}", token):
        raise ConfigError(f"Заполните {prefix}_BOT_TOKEN в .env токеном от BotFather.")
    other = os.environ.get("BUYBACK_BOT_TOKEN" if kind == "store" else "STORE_BOT_TOKEN", "").strip()
    if other and other == token:
        raise ConfigError("Магазину и скупке нужны два разных Telegram-бота.")
    ids = user_ids(os.environ.get(prefix + "_TESTER_IDS", ""), prefix + "_TESTER_IDS")
    return BotConfig(kind, token, ids, store_path if kind == "store" else buyer_path)
