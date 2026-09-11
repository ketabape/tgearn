"""Independent, synthetic-only acceptance ledger for the local bot demo.

This module never contacts a token issuer, payment provider, or the shop. Only
the static LAB_RAW_ fixtures can be registered through its public API. A lookup
means "present in this local registry", not "valid with an external service".
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from pathlib import Path
from typing import Any

from .db import Database, DomainError


_QUOTE_LIFETIME_SECONDS = 300
_TOKEN_PATTERN = re.compile(r"LAB_RAW_[A-Za-z0-9_]{8,96}\Z")
_QUOTE_PATTERN = re.compile(r"[0-9a-f]{32}\Z")

_OFFERS = (
    {"sku": "A50", "title": "Учебный пакет $50", "amount": 1800},
    {"sku": "A100", "title": "Учебный пакет $100", "amount": 3000},
)

_FIXTURES = (
    {"token": "LAB_RAW_P7x4Qm9Lk2Zr5Bv8", "sku": "A50"},
    {"token": "LAB_RAW_aN6tR3pY8sW2dF9g", "sku": "A100"},
    {"token": "LAB_RAW_A50_0001_G7bK9mQ2", "sku": "A50"},
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS buyback_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    available INTEGER NOT NULL CHECK (available IN (0, 1))
);
INSERT OR IGNORE INTO buyback_settings(id, available) VALUES (1, 1);

CREATE TABLE IF NOT EXISTS buyback_registry (
    token_hash TEXT PRIMARY KEY CHECK (length(token_hash) = 64),
    registry_id TEXT NOT NULL UNIQUE,
    sku TEXT NOT NULL,
    masked_token TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS buyback_quotes (
    id TEXT PRIMARY KEY CHECK (length(id) = 32),
    user_id INTEGER NOT NULL CHECK (user_id > 0),
    token_hash TEXT NOT NULL,
    registry_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    title TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    masked_token TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS buyback_quotes_user
    ON buyback_quotes(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS buyback_claims (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL CHECK (user_id > 0),
    token_hash TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
    quote_id TEXT NOT NULL UNIQUE,
    sku TEXT NOT NULL,
    title TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    masked_token TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status = 'accepted'),
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS buyback_claims_user
    ON buyback_claims(user_id, created_at DESC);
"""


def _require_user_id(user_id: int) -> None:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise DomainError("Не удалось определить пользователя.")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _mask_token(token: str) -> str:
    return "LAB_RAW_…" + token[-4:]


def _offer_for(sku: str) -> dict[str, Any] | None:
    return next((offer for offer in _OFFERS if offer["sku"] == sku), None)


def _claim_dict(row: Any) -> dict[str, Any]:
    return {
        key: row[key]
        for key in ("id", "sku", "title", "amount", "masked_token", "status")
    }


class BuybackRepository:
    """Own registry and accounting database; no shop or order dependencies."""

    def __init__(self, db_path: str | Path):
        self.db = Database(db_path)
        self.db.initialize(_SCHEMA)

    def offers(self) -> list[dict[str, Any]]:
        return [dict(offer) for offer in _OFFERS]

    def fixtures(self) -> list[dict[str, str]]:
        """Return the known, deliberately synthetic sample inputs."""
        return [dict(fixture) for fixture in _FIXTURES]

    def seed_registry(self) -> int:
        """Add static samples. Repeating this does not reset accepted claims."""
        inserted = 0
        with self.db.transaction() as conn:
            for fixture in _FIXTURES:
                token = fixture["token"]
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO buyback_registry "
                    "(token_hash, registry_id, sku, masked_token) VALUES (?, ?, ?, ?)",
                    (
                        _token_hash(token),
                        secrets.token_hex(16),
                        fixture["sku"],
                        _mask_token(token),
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def clear_registry(self) -> None:
        # Keeping claims preserves both history and global duplicate protection.
        # Quotes remain so confirmation can report the missing registry entry.
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM buyback_registry")

    def set_available(self, available: bool) -> None:
        if not isinstance(available, bool):
            raise DomainError("Укажите, доступна ли проверка.")
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE buyback_settings SET available = ? WHERE id = 1",
                (int(available),),
            )

    def registry_state(self) -> dict[str, Any]:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT available, "
                "(SELECT COUNT(*) FROM buyback_registry) AS count "
                "FROM buyback_settings WHERE id = 1"
            ).fetchone()
            return {"count": row["count"], "available": bool(row["available"])}

    def balance(self, user_id: int) -> int:
        """Return an illustrative ledger total; it cannot be paid or withdrawn."""
        _require_user_id(user_id)
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS total "
                "FROM buyback_claims WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return int(row["total"])

    def check(self, user_id: int, raw_token: str) -> dict[str, Any]:
        """Look up a synthetic input and prepare a five-minute confirmation.

        Invalid and unknown inputs are never stored. The only stored token
        representations are a SHA-256 hash and a short display mask.
        """
        _require_user_id(user_id)
        if not isinstance(raw_token, str):
            return {"kind": "format"}
        if len(raw_token) > 256:
            return {"kind": "format"}
        token = raw_token.strip()
        if not token:
            return {"kind": "empty"}
        if not _TOKEN_PATTERN.fullmatch(token):
            return {"kind": "format"}
        token_hash = _token_hash(token)

        with self.db.transaction() as conn:
            if not conn.execute(
                "SELECT available FROM buyback_settings WHERE id = 1"
            ).fetchone()["available"]:
                return {"kind": "unavailable"}
            if conn.execute(
                "SELECT 1 FROM buyback_claims WHERE token_hash = ?", (token_hash,)
            ).fetchone():
                return {"kind": "duplicate"}
            registered = conn.execute(
                "SELECT * FROM buyback_registry WHERE token_hash = ?", (token_hash,)
            ).fetchone()
            if registered is None:
                return {"kind": "notfound"}
            offer = _offer_for(registered["sku"])
            if offer is None:
                return {"kind": "unsupported"}

            quote_id = secrets.token_hex(16)
            now = time.time()
            conn.execute(
                "INSERT INTO buyback_quotes "
                "(id, user_id, token_hash, registry_id, sku, title, amount, "
                "masked_token, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    quote_id,
                    user_id,
                    token_hash,
                    registered["registry_id"],
                    offer["sku"],
                    offer["title"],
                    offer["amount"],
                    registered["masked_token"],
                    now,
                    now + _QUOTE_LIFETIME_SECONDS,
                ),
            )
            return {
                "kind": "matched",
                "quote_id": quote_id,
                "sku": offer["sku"],
                "title": offer["title"],
                "amount": offer["amount"],
                "masked_token": registered["masked_token"],
            }

    def accept(self, user_id: int, quote_id: str) -> dict[str, Any]:
        """Confirm a current, owned quote and credit the sample ledger once."""
        _require_user_id(user_id)
        if not isinstance(quote_id, str) or not _QUOTE_PATTERN.fullmatch(quote_id):
            return {"kind": "notfound"}

        with self.db.transaction() as conn:
            quote = conn.execute(
                "SELECT * FROM buyback_quotes WHERE id = ? AND user_id = ?",
                (quote_id, user_id),
            ).fetchone()
            if quote is None:
                return {"kind": "notfound"}
            if not conn.execute(
                "SELECT available FROM buyback_settings WHERE id = 1"
            ).fetchone()["available"]:
                return {"kind": "unavailable"}
            if conn.execute(
                "SELECT 1 FROM buyback_claims WHERE token_hash = ?",
                (quote["token_hash"],),
            ).fetchone():
                return {"kind": "duplicate"}
            now = time.time()
            if quote["expires_at"] <= now:
                return {"kind": "expired"}
            registered = conn.execute(
                "SELECT * FROM buyback_registry WHERE token_hash = ?",
                (quote["token_hash"],),
            ).fetchone()
            if registered is None:
                return {"kind": "notfound"}
            # A removed-and-reinserted entry is a new entry. An old quote cannot
            # silently survive that reset, a changed SKU, or a changed amount.
            offer = _offer_for(registered["sku"])
            if (
                registered["registry_id"] != quote["registry_id"]
                or registered["sku"] != quote["sku"]
                or registered["masked_token"] != quote["masked_token"]
                or offer is None
                or offer["amount"] != quote["amount"]
                or offer["title"] != quote["title"]
            ):
                return {"kind": "expired"}

            claim_id = secrets.token_hex(8)
            # BEGIN IMMEDIATE serializes the duplicate check and insertion;
            # the UNIQUE token_hash constraint also enforces this in SQLite.
            conn.execute(
                "INSERT INTO buyback_claims "
                "(id, user_id, token_hash, quote_id, sku, title, amount, "
                "masked_token, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?)",
                (
                    claim_id,
                    user_id,
                    quote["token_hash"],
                    quote_id,
                    quote["sku"],
                    quote["title"],
                    quote["amount"],
                    quote["masked_token"],
                    now,
                ),
            )
            claim = conn.execute(
                "SELECT * FROM buyback_claims WHERE id = ?", (claim_id,)
            ).fetchone()
            return {"kind": "accepted", "claim": _claim_dict(claim)}

    def claims(self, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        _require_user_id(user_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise DomainError("Количество записей должно быть от 1 до 100.")
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM buyback_claims WHERE user_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
            return [_claim_dict(row) for row in rows]
