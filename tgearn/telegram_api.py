"""A narrow Telegram Bot API client. No payment or third-party-key methods."""

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TelegramError(Exception):
    def __init__(self, code=0, retry_after=0, not_modified=False):
        self.code = code
        self.retry_after = retry_after
        self.not_modified = not_modified
        # Never include URLs, bot tokens, response text, or submitted messages.
        super().__init__(f"Telegram error {code}")


class TelegramAPI:
    METHODS = frozenset({
        "getMe", "getWebhookInfo", "getUpdates", "sendMessage",
        "editMessageText", "answerCallbackQuery", "setMyCommands",
    })

    def __init__(self, token):
        self._token = token

    def call(self, method, payload=None):
        if method not in self.METHODS:
            raise ValueError("This method is not part of the demonstration transport.")
        request = Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=35) as response:
                result = json.load(response)
        except HTTPError as error:
            try:
                result = json.loads(error.read(16384))
            except (ValueError, OSError):
                raise TelegramError(error.code) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise TelegramError() from None
        if not isinstance(result, dict):
            raise TelegramError()
        if not result.get("ok"):
            parameters = result.get("parameters")
            if not isinstance(parameters, dict):
                parameters = {}
            retry_after = parameters.get("retry_after", 0)
            if not isinstance(retry_after, int):
                retry_after = 0
            code = result.get("error_code", 0)
            if type(code) is not int:
                code = 0
            raise TelegramError(
                code,
                max(0, min(retry_after, 300)),
                "message is not modified" in str(result.get("description", "")).lower(),
            )
        return result.get("result")
