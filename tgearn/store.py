"""An isolated shop for the closed lab: synthetic goods and manual events only.

No network calls or payment providers live in this module. Amounts are whole
demonstration rubles. ``held`` on an order is the original balance contribution;
``balance()["held"]`` is the sum that is still reserved right now.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from .db import Database, DomainError


MIN_TOPUP = 1_000
MAX_TOPUP = 1_000_000_000
_MAX_SQLITE_INT = 2**63 - 1

_PRODUCTS = (
    ("A50", "Учебный пакет $50", "Текст", 50, 1_200),
    ("A100", "Учебный пакет $100", "Текст", 100, 2_000),
    ("T30", "Учебный текстовый пакет $30", "Текст", 30, 1_000),
    ("V50", "Учебный пакет изображений $50", "Изображения", 50, 1_300),
    ("S50", "Учебный звуковой пакет $50", "Аудио", 50, 1_500),
    ("V80", "Учебный видеопакет $80", "Видео", 80, 1_900),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS store_products (
    sku TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    nominal INTEGER NOT NULL CHECK (nominal > 0),
    price INTEGER NOT NULL CHECK (price > 0)
);
CREATE TABLE IF NOT EXISTS store_wallets (
    user_id INTEGER PRIMARY KEY,
    available INTEGER NOT NULL DEFAULT 0
        CHECK (available >= 0 AND typeof(available) = 'integer'),
    held INTEGER NOT NULL DEFAULT 0
        CHECK (held >= 0 AND typeof(held) = 'integer')
);
CREATE TABLE IF NOT EXISTS store_orders (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES store_wallets(user_id),
    sku TEXT NOT NULL REFERENCES store_products(sku),
    title TEXT NOT NULL,
    price INTEGER NOT NULL CHECK (price > 0),
    status TEXT NOT NULL
        CHECK (status IN ('awaiting', 'issued', 'cancelled', 'expired')),
    held INTEGER NOT NULL CHECK (held >= 0),
    delta INTEGER NOT NULL CHECK (delta >= 0),
    review INTEGER NOT NULL DEFAULT 0 CHECK (review IN (0, 1)),
    created_at INTEGER NOT NULL,
    CHECK (held + delta = price)
);
CREATE TABLE IF NOT EXISTS store_inventory (
    token TEXT PRIMARY KEY,
    sku TEXT NOT NULL REFERENCES store_products(sku),
    serial INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'available'
        CHECK (state IN ('available', 'reserved', 'issued')),
    order_id TEXT UNIQUE REFERENCES store_orders(id),
    UNIQUE (sku, serial),
    CHECK ((state = 'available' AND order_id IS NULL)
        OR (state IN ('reserved', 'issued') AND order_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS store_inventory_available
    ON store_inventory(sku, state, serial);
CREATE TABLE IF NOT EXISTS store_invoices (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES store_wallets(user_id),
    amount INTEGER NOT NULL CHECK (amount >= 1000),
    purpose TEXT NOT NULL CHECK (purpose IN ('purchase', 'topup')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'paid', 'cancelled', 'expired', 'review')),
    order_id TEXT UNIQUE REFERENCES store_orders(id),
    created_at INTEGER NOT NULL,
    CHECK ((purpose = 'topup' AND order_id IS NULL)
        OR (purpose = 'purchase' AND order_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS store_one_pending_invoice
    ON store_invoices(user_id) WHERE status = 'pending';
CREATE TABLE IF NOT EXISTS store_requests (
    request_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES store_wallets(user_id),
    action TEXT NOT NULL CHECK (action IN ('checkout', 'topup')),
    sku TEXT REFERENCES store_products(sku),
    amount INTEGER NOT NULL,
    result_type TEXT NOT NULL CHECK (result_type IN ('minimum', 'order', 'invoice')),
    order_id TEXT REFERENCES store_orders(id),
    invoice_id TEXT REFERENCES store_invoices(id),
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS store_orders_user ON store_orders(user_id, created_at);
CREATE INDEX IF NOT EXISTS store_invoices_user ON store_invoices(user_id, created_at);
"""


def _id() -> str:
    return uuid.uuid4().hex


def _now() -> int:
    return time.time_ns() // 1_000_000


def _user(user_id: int) -> None:
    if type(user_id) is not int or not 0 < user_id <= _MAX_SQLITE_INT:
        raise DomainError("Не удалось определить пользователя.")


def _request(request_id: str) -> None:
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        raise DomainError("Кнопка устарела. Откройте раздел заново.")


def _page(limit: int, offset: int = 0) -> None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise DomainError("Можно показать от 1 до 100 записей за раз.")
    if type(offset) is not int or not 0 <= offset <= _MAX_SQLITE_INT:
        raise DomainError("Не удалось открыть эту страницу.")


class StoreRepository:
    """Persistent, transactional state of one demonstration shop."""

    def __init__(self, db_path: str):
        self.db = Database(db_path)
        self.db.initialize(_SCHEMA)
        with self.db.transaction() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO store_products "
                "(sku, title, category, nominal, price) VALUES (?, ?, ?, ?, ?)",
                _PRODUCTS,
            )
            connection.executemany(
                "INSERT OR IGNORE INTO store_inventory (token, sku, serial) VALUES (?, ?, ?)",
                (
                    (f"LAB_RAW_{sku}_{number:04d}_G7bK9mQ2", sku, number)
                    for sku, *_ in _PRODUCTS
                    for number in range(1, 501)
                ),
            )

    def catalog(self) -> list[dict[str, Any]]:
        with self.db.read() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT p.sku, p.title, p.category, p.nominal, p.price, "
                    "COUNT(i.token) AS stock FROM store_products p "
                    "LEFT JOIN store_inventory i ON i.sku = p.sku AND i.state = ? "
                    "GROUP BY p.sku ORDER BY p.rowid",
                    ("available",),
                ).fetchall()
            ]

    def balance(self, user_id: int) -> dict[str, int]:
        _user(user_id)
        with self.db.read() as connection:
            row = connection.execute(
                "SELECT available, held FROM store_wallets WHERE user_id = ?", (user_id,)
            ).fetchone()
            return dict(row) if row else {"available": 0, "held": 0}

    def checkout(
        self,
        user_id: int,
        sku: str,
        request_id: str,
        allow_minimum_topup: bool = False,
    ) -> dict[str, Any]:
        _user(user_id)
        _request(request_id)
        if not isinstance(sku, str) or not sku or len(sku) > 32:
            raise DomainError("Товар не найден.")
        if type(allow_minimum_topup) is not bool:
            raise DomainError("Подтвердите сумму пополнения кнопкой.")

        with self.db.transaction() as connection:
            product = connection.execute(
                "SELECT * FROM store_products WHERE sku = ?", (sku,)
            ).fetchone()
            if product is None:
                raise DomainError("Товар не найден.")
            prior = self._prior_request(
                connection, user_id, request_id, "checkout", sku, product["price"]
            )
            if prior is not None and prior["result_type"] == "order":
                return self._checkout_result(connection, prior["order_id"])

            self._ensure_wallet(connection, user_id)
            wallet = connection.execute(
                "SELECT available, held FROM store_wallets WHERE user_id = ?", (user_id,)
            ).fetchone()
            stock = connection.execute(
                "SELECT token FROM store_inventory WHERE sku = ? AND state = ? "
                "ORDER BY serial LIMIT 1",
                (sku, "available"),
            ).fetchone()
            if stock is None:
                raise DomainError("Этот товар закончился. Выберите другой.")

            held = min(wallet["available"], product["price"])
            delta = product["price"] - held
            if delta:
                self._ensure_no_pending(connection, user_id)
                if prior is not None and prior["result_type"] == "minimum" and delta > MIN_TOPUP:
                    raise DomainError("Баланс изменился. Откройте товар и подтвердите оплату заново.")
                if delta < MIN_TOPUP and not allow_minimum_topup:
                    if prior is None:
                        self._save_request(
                            connection, request_id, user_id, "checkout", sku,
                            product["price"], "minimum", None, None,
                        )
                    return {
                        "kind": "minimum", "sku": sku, "delta": delta,
                        "amount": MIN_TOPUP, "after": MIN_TOPUP - delta,
                    }

            order_id = _id()
            status = "awaiting" if delta else "issued"
            connection.execute(
                "INSERT INTO store_orders "
                "(id, user_id, sku, title, price, status, held, delta, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (order_id, user_id, sku, product["title"], product["price"],
                 status, held, delta, _now()),
            )
            connection.execute(
                "UPDATE store_wallets SET available = available - ?, held = held + ? "
                "WHERE user_id = ?",
                (held, held if delta else 0, user_id),
            )
            changed = connection.execute(
                "UPDATE store_inventory SET state = ?, order_id = ? "
                "WHERE token = ? AND state = ? AND order_id IS NULL",
                ("reserved" if delta else "issued", order_id, stock["token"], "available"),
            ).rowcount
            if changed != 1:
                raise DomainError("Товар уже забрали. Обновите каталог.")

            invoice_id = None
            if delta:
                invoice_id = _id()
                connection.execute(
                    "INSERT INTO store_invoices "
                    "(id, user_id, amount, purpose, status, order_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (invoice_id, user_id, max(MIN_TOPUP, delta),
                     "purchase", "pending", order_id, _now()),
                )

            if prior is None:
                self._save_request(
                    connection, request_id, user_id, "checkout", sku,
                    product["price"], "order", order_id, invoice_id,
                )
            else:
                connection.execute(
                    "UPDATE store_requests SET result_type = ?, order_id = ?, invoice_id = ? "
                    "WHERE request_id = ?",
                    ("order", order_id, invoice_id, request_id),
                )
            return self._checkout_result(connection, order_id)

    def create_topup(self, user_id: int, amount: int, request_id: str) -> dict[str, Any]:
        _user(user_id)
        _request(request_id)
        if type(amount) is not int or not MIN_TOPUP <= amount <= MAX_TOPUP:
            raise DomainError("Введите целую сумму от 1 000 до 1 000 000 000 ₽.")

        with self.db.transaction() as connection:
            prior = self._prior_request(connection, user_id, request_id, "topup", None, amount)
            if prior is not None:
                return {"kind": "invoice", "invoice": self._invoice_dict(
                    self._get_invoice(connection, user_id, prior["invoice_id"])
                )}
            self._ensure_wallet(connection, user_id)
            self._ensure_no_pending(connection, user_id)
            invoice_id = _id()
            connection.execute(
                "INSERT INTO store_invoices "
                "(id, user_id, amount, purpose, status, order_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (invoice_id, user_id, amount, "topup", "pending", None, _now()),
            )
            self._save_request(
                connection, request_id, user_id, "topup", None, amount,
                "invoice", None, invoice_id,
            )
            return {"kind": "invoice", "invoice": self._invoice_dict(
                self._get_invoice(connection, user_id, invoice_id)
            )}

    def settle_invoice(self, user_id: int, invoice_id: str, event: str) -> dict[str, Any]:
        """Apply an explicitly manual lab event, never a provider callback."""
        _user(user_id)
        if not isinstance(event, str) or event not in ("paid", "cancelled", "expired"):
            raise DomainError("Неизвестное событие счёта.")
        with self.db.transaction() as connection:
            invoice = self._get_invoice(connection, user_id, invoice_id)
            if invoice["status"] != "pending":
                if event == "paid" and invoice["status"] in ("cancelled", "expired"):
                    connection.execute(
                        "UPDATE store_invoices SET status = ? WHERE id = ?", ("review", invoice_id)
                    )
                    if invoice["order_id"] is not None:
                        connection.execute(
                            "UPDATE store_orders SET review = ? WHERE id = ?",
                            (1, invoice["order_id"]),
                        )
                    return self._settlement_result(connection, user_id, invoice_id, "review")
                return self._settlement_result(connection, user_id, invoice_id, "duplicate")

            order = None
            if invoice["order_id"] is not None:
                order = connection.execute(
                    "SELECT * FROM store_orders WHERE id = ? AND user_id = ?",
                    (invoice["order_id"], user_id),
                ).fetchone()
                if order is None or order["status"] != "awaiting":
                    raise DomainError("Не удалось завершить заказ. Нужна проверка администратора.")

            if event == "paid":
                if order is None:
                    connection.execute(
                        "UPDATE store_wallets SET available = available + ? WHERE user_id = ?",
                        (invoice["amount"], user_id),
                    )
                    kind = "credited"
                else:
                    changed = connection.execute(
                        "UPDATE store_inventory SET state = ? WHERE order_id = ? AND state = ?",
                        ("issued", order["id"], "reserved"),
                    ).rowcount
                    if changed != 1 or invoice["amount"] < order["delta"]:
                        raise DomainError("Не удалось выдать товар. Нужна проверка администратора.")
                    connection.execute(
                        "UPDATE store_wallets SET held = held - ?, available = available + ? "
                        "WHERE user_id = ?",
                        (order["held"], invoice["amount"] - order["delta"], user_id),
                    )
                    connection.execute(
                        "UPDATE store_orders SET status = ? WHERE id = ?", ("issued", order["id"])
                    )
                    kind = "issued"
            else:
                if order is not None:
                    changed = connection.execute(
                        "UPDATE store_inventory SET state = ?, order_id = NULL "
                        "WHERE order_id = ? AND state = ?",
                        ("available", order["id"], "reserved"),
                    ).rowcount
                    if changed != 1:
                        raise DomainError("Не удалось отменить заказ. Нужна проверка администратора.")
                    connection.execute(
                        "UPDATE store_wallets SET available = available + ?, held = held - ? "
                        "WHERE user_id = ?",
                        (order["held"], order["held"], user_id),
                    )
                    connection.execute(
                        "UPDATE store_orders SET status = ? WHERE id = ?", (event, order["id"])
                    )
                kind = event

            connection.execute(
                "UPDATE store_invoices SET status = ? WHERE id = ?", (event, invoice_id)
            )
            return self._settlement_result(connection, user_id, invoice_id, kind)

    def orders(self, user_id: int, limit: int = 10, offset: int = 0) -> list[dict[str, Any]]:
        _user(user_id)
        _page(limit, offset)
        with self.db.read() as connection:
            rows = connection.execute(
                "SELECT * FROM store_orders WHERE user_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (user_id, limit, offset),
            ).fetchall()
            return [self._order_dict(connection, row) for row in rows]

    def invoice(self, user_id: int, invoice_id: str) -> dict[str, Any]:
        _user(user_id)
        with self.db.read() as connection:
            return self._invoice_dict(self._get_invoice(connection, user_id, invoice_id))

    def invoices(self, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        _user(user_id)
        _page(limit)
        with self.db.read() as connection:
            rows = connection.execute(
                "SELECT * FROM store_invoices WHERE user_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
            return [self._invoice_dict(row) for row in rows]

    @staticmethod
    def _ensure_wallet(connection, user_id: int) -> None:
        connection.execute("INSERT OR IGNORE INTO store_wallets (user_id) VALUES (?)", (user_id,))

    @staticmethod
    def _ensure_no_pending(connection, user_id: int) -> None:
        pending = connection.execute(
            "SELECT id FROM store_invoices WHERE user_id = ? AND status = ? LIMIT 1",
            (user_id, "pending"),
        ).fetchone()
        if pending is not None:
            raise DomainError("У вас уже есть неоплаченный счёт. Завершите или отмените его в разделе «Счета».")

    @staticmethod
    def _prior_request(connection, user_id, request_id, action, sku, amount):
        row = connection.execute(
            "SELECT * FROM store_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is not None and (
            row["user_id"] != user_id or row["action"] != action
            or row["sku"] != sku or row["amount"] != amount
        ):
            raise DomainError("Эта кнопка относится к другому действию. Откройте раздел заново.")
        return row

    @staticmethod
    def _save_request(connection, request_id, user_id, action, sku, amount,
                      result_type, order_id, invoice_id) -> None:
        connection.execute(
            "INSERT INTO store_requests "
            "(request_id, user_id, action, sku, amount, result_type, order_id, invoice_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request_id, user_id, action, sku, amount, result_type, order_id, invoice_id, _now()),
        )

    @staticmethod
    def _get_invoice(connection, user_id, invoice_id):
        if not isinstance(invoice_id, str) or not invoice_id or len(invoice_id) > 128:
            raise DomainError("Счёт не найден.")
        row = connection.execute(
            "SELECT * FROM store_invoices WHERE id = ? AND user_id = ?", (invoice_id, user_id)
        ).fetchone()
        if row is None:
            raise DomainError("Счёт не найден.")
        return row

    @staticmethod
    def _invoice_dict(row) -> dict[str, Any]:
        return {key: row[key] for key in ("id", "amount", "purpose", "status", "order_id")}

    @staticmethod
    def _order_dict(connection, row) -> dict[str, Any]:
        result = {key: row[key] for key in ("id", "sku", "title", "price", "status", "held", "delta")}
        result["review"] = bool(row["review"])
        result["token"] = None
        if row["status"] == "issued":
            token = connection.execute(
                "SELECT token FROM store_inventory WHERE order_id = ? AND state = ?",
                (row["id"], "issued"),
            ).fetchone()
            if token is None:
                raise DomainError("Не удалось открыть товар. Нужна проверка администратора.")
            result["token"] = token["token"]
        return result

    def _checkout_result(self, connection, order_id) -> dict[str, Any]:
        order = connection.execute("SELECT * FROM store_orders WHERE id = ?", (order_id,)).fetchone()
        if order is None:
            raise DomainError("Заказ не найден.")
        invoice = connection.execute(
            "SELECT * FROM store_invoices WHERE order_id = ?", (order_id,)
        ).fetchone()
        return {
            "kind": "order", "order": self._order_dict(connection, order),
            "invoice": self._invoice_dict(invoice) if invoice is not None else None,
        }

    def _settlement_result(self, connection, user_id, invoice_id, kind) -> dict[str, Any]:
        invoice = self._get_invoice(connection, user_id, invoice_id)
        result = {"kind": kind, "invoice": self._invoice_dict(invoice), "order": None}
        if invoice["order_id"] is not None:
            order = connection.execute(
                "SELECT * FROM store_orders WHERE id = ? AND user_id = ?",
                (invoice["order_id"], user_id),
            ).fetchone()
            if order is None:
                raise DomainError("Заказ не найден.")
            result["order"] = self._order_dict(connection, order)
        return result
