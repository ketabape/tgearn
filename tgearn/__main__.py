"""Entry point: python -m tgearn store|buyback [--check]."""

import argparse
import sys

from .config import read_config
from .runtime import run


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error can echo supplied argument values or secrets.
        self.print_usage(sys.stderr)
        self.exit(2, "Выберите store или buyback. Для проверки настроек добавьте --check.\n")


def main(argv=None) -> int:
    parser = _ArgumentParser(
        prog="python -m tgearn",
        description="Два независимых Telegram-бота для закрытой демонстрации.",
    )
    parser.add_argument("kind", choices=("store", "buyback"), help="магазин или приёмка")
    parser.add_argument("--check", action="store_true", help="проверить настройки без подключения к Telegram")
    args = parser.parse_args(argv)
    try:
        config = read_config(args.kind)
    except KeyboardInterrupt:
        print("Проверка остановлена.")
        return 0
    except Exception:
        print(
            "Не удалось прочитать настройки. Проверьте .env: токены ботов, числовые ID и разные пути баз.",
            file=sys.stderr,
        )
        return 2
    if args.check:
        label = "магазина" if args.kind == "store" else "приёмки"
        print(f"Настройки {label} верны. Подключения к Telegram не было.")
        if not config.allowed_ids:
            print("Список тестировщиков пуст: после запуска будет доступна только команда /id.")
        return 0
    try:
        return run(config)
    except KeyboardInterrupt:
        print("Бот остановлен.")
        return 0
    except Exception:
        print("Не удалось запустить бот. Проверьте настройки и доступ к папке данных.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
