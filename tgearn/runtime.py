"""Conservative long polling for the closed, synthetic-only Telegram demo.

Every well-identified update gets one handling attempt, then its next offset is
saved even if a response could not be delivered. A successful ledger mutation
is available in history; delivery failures do not repeat mutations. A hard crash
between a mutation and offset persistence can still replay the update, so the
repositories/controllers must also use durable operation idempotency keys.

Incoming updates, submitted text, URLs, tokens, and exception messages are never
logged here. A malformed batch without usable update IDs stops polling because
there is no safe offset to acknowledge it with.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from .buyback import BuybackRepository
from .db import UpdateCursor
from .process_lock import process_lock
from .store import StoreRepository
from .telegram_api import TelegramAPI, TelegramError


_COMMANDS = [
    {"command": "start", "description": "Главное меню"},
    {"command": "id", "description": "Мой Telegram ID"},
    {"command": "help", "description": "Помощь"},
    {"command": "balance", "description": "Мой баланс"},
]
_STORE_COMMANDS = [
    {"command": "catalog", "description": "Каталог"},
    {"command": "orders", "description": "Мои заказы"},
    {"command": "invoices", "description": "Мои счета"},
]
_BUYBACK_COMMANDS = [
    {"command": "check", "description": "Проверить учебный токен"},
    {"command": "claims", "description": "История приёмки"},
    {"command": "fixtures", "description": "Примеры для проверки"},
]
_TRANSIENT_CODES = {0, 408, 425, 429}
_MAX_UPDATE_ID = 2**63 - 2


class _StopRequested(Exception):
    pass


class _RunFailed(Exception):
    pass


def _safe_retry_after(error: TelegramError) -> int:
    value = error.retry_after
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, min(value, 300))


def _transient(error: TelegramError) -> bool:
    code = error.code
    return isinstance(code, int) and (code in _TRANSIENT_CODES or 500 <= code <= 599)


def _wait(stop_event: Any, seconds: int) -> None:
    """Honor rate limits while allowing stop requests during long delays."""
    remaining = seconds
    while remaining > 0:
        chunk = min(remaining, 60)
        if stop_event.wait(chunk):
            raise _StopRequested()
        remaining -= chunk


def _fatal_telegram(error: TelegramError, emit: Callable[[str], None]) -> bool:
    if error.code == 401:
        emit("Telegram не принял токен бота. Проверьте токен в .env.")
        return True
    if error.code == 409:
        emit("Этот бот уже получает сообщения другим способом. Остановите другой запуск.")
        return True
    return False


def _call_with_retry(api, method, payload, stop_event, emit):
    backoff = 1
    while not stop_event.is_set():
        try:
            return api.call(method, payload)
        except TelegramError as error:
            if _fatal_telegram(error, emit):
                raise _RunFailed() from None
            if not _transient(error):
                emit("Telegram отклонил запрос. Бот остановлен; проверьте его настройки.")
                raise _RunFailed() from None
            delay = max(backoff, _safe_retry_after(error))
            if error.code == 429:
                emit(f"Telegram временно ограничил запросы. Повтор через {delay} с.")
            else:
                emit(f"Нет связи с Telegram. Повтор через {delay} с.")
        except (OSError, TimeoutError):
            delay = backoff
            emit(f"Нет связи с Telegram. Повтор через {delay} с.")
        _wait(stop_event, delay)
        backoff = min(backoff * 2, 30)
    raise _StopRequested()


def _validated_updates(result, emit):
    if not isinstance(result, list):
        emit("Telegram вернул неожиданный ответ. Бот остановлен без обработки сообщений.")
        raise _RunFailed()
    for update in result:
        if (
            not isinstance(update, dict)
            or type(update.get("update_id")) is not int
            or not 0 <= update["update_id"] <= _MAX_UPDATE_ID
        ):
            emit("Не удалось определить номер сообщения. Бот остановлен без пропуска очереди.")
            raise _RunFailed()
    return sorted(result, key=lambda update: update["update_id"])


def _handle_once(controller, update, cursor, stop_event, emit):
    fatal_error = None
    delay = 0
    try:
        controller.handle(update)
    except TelegramError as error:
        if not error.not_modified:
            emit("Ответ не доставлен. Если действие завершилось, его результат есть в истории.")
        if error.code in {401, 409}:
            fatal_error = error
        elif _transient(error):
            delay = max(1, _safe_retry_after(error))
    except Exception:
        emit("Не удалось обработать сообщение. Откройте меню и проверьте историю перед повтором.")
    finally:
        # Do not retry the whole update just because sending its response failed.
        # If persisting this cursor fails, run() stops before fetching any more.
        cursor.advance(update["update_id"])
    if fatal_error is not None:
        _fatal_telegram(fatal_error, emit)
        raise _RunFailed()
    if delay:
        _wait(stop_event, delay)


def _run_locked(config, api, controller_factory, stop_event, emit):
    me = _call_with_retry(api, "getMe", {}, stop_event, emit)
    if not isinstance(me, dict) or me.get("is_bot") is not True:
        emit("Telegram не подтвердил учётную запись бота. Запуск остановлен.")
        raise _RunFailed()
    webhook = _call_with_retry(api, "getWebhookInfo", {}, stop_event, emit)
    if not isinstance(webhook, dict) or not isinstance(webhook.get("url"), str):
        emit("Не удалось проверить способ получения сообщений. Запуск остановлен.")
        raise _RunFailed()
    if webhook["url"]:
        emit("Для этого бота настроен webhook. Остановите прежний способ получения сообщений.")
        raise _RunFailed()

    if config.kind == "store":
        repository = StoreRepository(config.database)
        commands = _COMMANDS + _STORE_COMMANDS
    elif config.kind == "buyback":
        repository = BuybackRepository(config.database)
        commands = _COMMANDS + _BUYBACK_COMMANDS
    else:
        emit("Неизвестный вид бота. Выберите store или buyback.")
        raise _RunFailed()
    cursor = UpdateCursor(repository.db)
    if controller_factory is None:
        from .bots import make_controller

        controller_factory = make_controller
    controller = controller_factory(config, api, repository)
    _call_with_retry(
        api, "setMyCommands", {"commands": [dict(command) for command in commands]},
        stop_event, emit,
    )
    emit("Бот запущен в закрытом демонстрационном режиме. Для остановки нажмите Ctrl+C.")
    if not config.allowed_ids:
        emit("Список тестировщиков пуст: доступна только команда /id.")

    next_update = cursor.read()
    while not stop_event.is_set():
        result = _call_with_retry(
            api,
            "getUpdates",
            {
                "offset": next_update,
                "timeout": 20,
                "limit": 100,
                "allowed_updates": ["message", "callback_query"],
            },
            stop_event,
            emit,
        )
        for update in _validated_updates(result, emit):
            if stop_event.is_set():
                raise _StopRequested()
            if update["update_id"] < next_update:
                continue
            _handle_once(controller, update, cursor, stop_event, emit)
            next_update = update["update_id"] + 1


def run(config, *, api=None, controller_factory=None, stop_event=None, emit=None) -> int:
    """Run one bot; dependencies can be supplied for network-free tests.

    ``controller_factory(config, api, repository)`` returns an object with
    ``handle(update)``. The factory/controller enforces the tester allowlist.
    Returns 0 for a requested stop and 1 for a sanitized operational failure.
    """
    stop_event = stop_event if stop_event is not None else threading.Event()
    emit = emit if emit is not None else print
    api = api if api is not None else TelegramAPI(config.token)
    try:
        with process_lock(config.database):
            try:
                _run_locked(config, api, controller_factory, stop_event, emit)
            except _StopRequested:
                emit("Бот остановлен.")
                return 0
            except _RunFailed:
                return 1
            except KeyboardInterrupt:
                emit("Бот остановлен.")
                return 0
            except Exception:
                emit("Бот остановлен из-за внутренней ошибки. Сохранённые операции доступны в истории.")
                return 1
    except KeyboardInterrupt:
        emit("Бот остановлен.")
        return 0
    except RuntimeError:
        emit("Этот бот уже запущен с той же базой. Остановите предыдущий процесс.")
        return 1
    except Exception:
        emit("Не удалось открыть базу или файл блокировки. Проверьте доступ к папке данных.")
        return 1
    emit("Бот остановлен.")
    return 0
