"""Private Telegram menus for two independent, synthetic-only demonstrations."""

from dataclasses import dataclass
from html import escape
import re
import uuid

from .db import DomainError
from .telegram_api import TelegramError


def money(amount):
    return f"{amount:,}".replace(",", " ") + " ₽"


def button(label, data):
    if not 1 <= len(data.encode("utf-8")) <= 64:
        raise ValueError("Callback data must fit Telegram's 64-byte limit.")
    return {"text": label, "callback_data": data}


def nonce():
    return uuid.uuid4().hex


def action_id(value):
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise DomainError("Эта кнопка устарела. Откройте раздел заново.")
    return value


@dataclass
class View:
    text: str
    rows: list


class Controller:
    def __init__(self, config, api, repository):
        self.config = config
        self.api = api
        self.repo = repository

    def send(self, chat_id, view, message_id=None):
        payload = {
            "chat_id": chat_id,
            "text": view.text,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": view.rows},
            "link_preview_options": {"is_disabled": True},
        }
        if message_id is not None:
            try:
                self.api.call("editMessageText", {**payload, "message_id": message_id})
                return
            except TelegramError as error:
                if error.not_modified:
                    return
                if error.code != 400:
                    raise
                # The menu might have been deleted or become too old to edit.
        self.api.call("sendMessage", payload)

    def handle(self, update):
        if not isinstance(update, dict):
            return
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            message = callback.get("message")
            sender = callback.get("from")
        else:
            callback = None
            message = update.get("message")
            sender = message.get("from") if isinstance(message, dict) else None
        if not isinstance(message, dict) or not isinstance(sender, dict):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("type") != "private":
            return
        user_id = sender.get("id")
        if (
            type(user_id) is not int or not 0 < user_id < 2**63
            or sender.get("is_bot") or chat.get("id") != user_id
        ):
            return
        text = message.get("text", "")
        if not isinstance(text, str):
            text = ""
        command = text.split(maxsplit=1)[0].split("@", 1)[0] if text.strip() else ""

        if callback:
            callback_id = callback.get("id")
            if not isinstance(callback_id, str) or len(callback_id) > 256:
                return
            try:
                self.api.call("answerCallbackQuery", {"callback_query_id": callback_id})
            except TelegramError as error:
                if error.code != 400:
                    raise
        elif command == "/id":
            self.send(user_id, View(f"Ваш Telegram ID: <code>{user_id}</code>", []))
            return

        if user_id not in self.config.allowed_ids:
            self.send(user_id, View(
                "Это закрытая демонстрация. Доступ есть только у участников проверки.\n\n"
                "Команда /id покажет ваш Telegram ID.", []
            ))
            return

        try:
            if callback:
                data = callback.get("data", "")
                if not isinstance(data, str) or not 1 <= len(data.encode("utf-8")) <= 64:
                    raise DomainError("Откройте меню командой /start.")
                view = self.on_action(user_id, data.split(":"))
            else:
                update_id = update.get("update_id")
                # Telegram supplies a stable integer, so a replay cannot create a second top-up.
                request_id = f"message-{user_id}-{update_id}" if type(update_id) is int else None
                view = self.on_text(user_id, text, command, request_id)
        except DomainError as error:
            view = View(escape(str(error)), self.home_rows())
        self.send(user_id, view, message.get("message_id") if callback else None)

    def home_rows(self):
        return [[button("В меню", "home")]]


class StoreController(Controller):
    def home(self):
        return View(
            "<b>Тестовый магазин</b>\n\n"
            "Здесь можно пройти покупку от выбора товара до выдачи. "
            "Товары и баланс учебные, деньги не переводятся.\n\n"
            "Выберите раздел:",
            [[button("Каталог", "catalog"), button("Баланс", "wallet")],
             [button("Мои заказы", "orders"), button("Счета", "invoices")],
             [button("Помощь", "help")]],
        )

    def catalog(self):
        rows = [[button(f"{p['title']} · {money(p['price'])}", f"item:{p['sku']}")]
                for p in self.repo.catalog()]
        return View(
            "<b>Тестовый каталог</b>\n\nТекст, изображения, аудио и видео. "
            "Все пакеты — примеры для проверки покупки. Они не дают доступа к внешним сервисам.",
            rows + self.home_rows(),
        )

    def product(self, sku):
        product = next((p for p in self.repo.catalog() if p["sku"] == sku), None)
        if not product:
            raise DomainError("Товар не найден. Откройте каталог заново.")
        return View(
            f"<b>{escape(product['title'])}</b>\n\n"
            f"Раздел: {escape(product['category'])}\n"
            f"Цена в демонстрации: {money(product['price'])}\n"
            f"Осталось примеров: {product['stock']}\n\n"
            "После тестовой оплаты вы получите учебный токен LAB_RAW_. "
            "Долларовый номинал — часть примера, не настоящий баланс API.",
            ([[button("К покупке", f"buy:{sku}:{nonce()}")]] if product["stock"] else [])
            + [[button("Назад к товарам", "catalog")]],
        )

    def wallet(self, user_id):
        balance = self.repo.balance(user_id)
        return View(
            f"<b>Учебный баланс</b>\n\nДоступно: {money(balance['available'])}\n"
            f"Отложено для текущего заказа: {money(balance['held'])}\n\n"
            "Пополнение здесь имитируется. Настоящие деньги не нужны.\n"
            "Другая сумма: <code>/topup 1500</code> (от 1 000 ₽).",
            [[button("+1 000 ₽", f"topup:1000:{nonce()}"),
              button("+2 000 ₽", f"topup:2000:{nonce()}")],
             [button("Открыть счета", "invoices")]] + self.home_rows(),
        )

    def order_text(self, order):
        statuses = {"awaiting": "ждёт тестовой оплаты", "issued": "выдан",
                    "cancelled": "отменён", "expired": "срок счёта истёк"}
        result = (
            f"<b>{escape(order['title'])}</b> · {money(order['price'])}\n"
            f"Заказ <code>{order['id'][:8]}</code>: {statuses[order['status']]}"
        )
        if order.get("review"):
            result += "\nПозднее событие оплаты: требуется разбор. Повторной выдачи нет."
        if order.get("token"):
            result += f"\nУчебный токен:\n<code>{escape(order['token'])}</code>"
        return result

    def order_view(self, order):
        return View(self.order_text(order),
                    [[button("Мои заказы", "orders"), button("Каталог", "catalog")]])

    def invoice_view(self, invoice, order=None):
        statuses = {"pending": "ожидает события", "paid": "учебная оплата подтверждена",
                    "cancelled": "отменён", "expired": "срок истёк", "review": "требуется разбор"}
        text = (f"<b>Тестовый счёт</b> <code>{invoice['id'][:8]}</code>\n\n"
                f"Сумма: {money(invoice['amount'])}\nСтатус: {statuses[invoice['status']]}\n")
        if order:
            text += (f"\nТовар: {escape(order['title'])}\n"
                     f"С учебного баланса: {money(order['held'])}\n"
                     f"Не хватает на товар: {money(order['delta'])}\n")
            surplus = invoice["amount"] - order["delta"]
            if surplus:
                text += f"После оплаты на балансе останется ещё {money(surplus)}.\n"
        text += "\nПлатёжная система не подключена. Кнопки ниже задают событие для проверки логики."
        rows = []
        if invoice["status"] == "pending":
            rows = [[button("Тест: подтвердить оплату", f"event:paid:{invoice['id']}")],
                    [button("Отменить", f"event:cancelled:{invoice['id']}"),
                     button("Тест: срок истёк", f"event:expired:{invoice['id']}")]]
        elif invoice["status"] in {"cancelled", "expired"}:
            rows = [[button("Тест: оплата после отмены", f"event:paid:{invoice['id']}")]]
        return View(text, rows + [[button("Счета", "invoices"), button("Баланс", "wallet")]]
                    + self.home_rows())

    def checkout(self, user_id, sku, request_id, minimum=False):
        result = self.repo.checkout(user_id, sku, action_id(request_id), minimum)
        if result["kind"] == "minimum":
            return View(
                f"<b>Подтвердите сумму</b>\n\nНа товар не хватает {money(result['delta'])}. "
                f"В этой модели минимальное пополнение — {money(result['amount'])}.\n\n"
                f"После покупки {money(result['after'])} останется на учебном балансе. "
                "Настоящего списания не будет.",
                [[button(f"Тест: пополнить на {money(result['amount'])}", f"minimum:{sku}:{request_id}")],
                 [button("Вернуться к товару", f"item:{sku}")]],
            )
        if result["order"]["status"] == "issued":
            return self.order_view(result["order"])
        if result.get("invoice"):
            return self.invoice_view(result["invoice"], result["order"])
        return self.order_view(result["order"])

    def orders(self, user_id, offset=0):
        orders = self.repo.orders(user_id, limit=8, offset=offset)
        text = "<b>Мои тестовые заказы</b>\n\n" + (
            "\n\n".join(self.order_text(order) for order in orders) if orders
            else "Здесь пока нет заказов. Начните с каталога."
        )
        rows = []
        navigation = []
        if offset:
            navigation.append(button("Новее", f"orders:{max(0, offset - 8)}"))
        if len(orders) == 8:
            navigation.append(button("Раньше", f"orders:{offset + 8}"))
        if navigation:
            rows.append(navigation)
        return View(text, rows + [[button("Каталог", "catalog"), button("Счета", "invoices")]]
                    + self.home_rows())

    def invoices(self, user_id):
        invoices = self.repo.invoices(user_id)
        return View(
            "<b>Последние тестовые счета</b>\n\n" + (
                "Откройте счёт, чтобы посмотреть результат или завершить проверку."
                if invoices else "Счетов пока нет. Их можно создать при покупке или пополнении."
            ),
            [[button(f"{invoice['id'][:8]} · {money(invoice['amount'])}", f"invoice:{invoice['id']}")]
             for invoice in invoices] + self.home_rows(),
        )

    def settle(self, user_id, event, invoice_id):
        if event not in {"paid", "cancelled", "expired"}:
            raise DomainError("Такого действия нет. Откройте счёт заново.")
        result = self.repo.settle_invoice(user_id, action_id(invoice_id), event)
        if result.get("order") and result["order"]["status"] == "issued":
            return self.order_view(result["order"])
        messages = {
            "credited": "Учебный баланс пополнен.",
            "cancelled": "Счёт отменён. Отложенный баланс и товар снова доступны.",
            "expired": "Срок тестового счёта истёк. Отложенный баланс и товар снова доступны.",
            "duplicate": "Это событие уже учтено. Повторного начисления нет.",
            "review": "Оплата пришла после закрытия счёта. Нужен разбор; начисления и выдачи нет.",
        }
        view = self.invoice_view(self.repo.invoice(user_id, invoice_id), result.get("order"))
        view.text = messages.get(result["kind"], "Событие учтено.") + "\n\n" + view.text
        return view

    def help(self):
        return View(
            "<b>Как проверить магазин</b>\n\n"
            "1. Выберите товар.\n2. Откройте тестовый счёт.\n"
            "3. Нажмите «Тест: подтвердить оплату».\n4. Найдите токен в «Моих заказах».\n\n"
            "Можно сначала пополнить учебный баланс. Если его не хватит, бот покажет доплату. "
            "Минимальная сумма в модели — 1 000 ₽; остаток остаётся у вас.\n\n"
            "Если ответ не пришёл, откройте «Мои заказы» или «Счета»: сохранённый результат не теряется.\n\n"
            "Это демонстрация: E-pay, настоящие товары, гарантии поставщиков и денежные переводы не подключены.",
            self.home_rows(),
        )

    def on_action(self, user_id, parts):
        action = parts[0]
        if len(parts) == 1:
            routes = {"home": self.home, "catalog": self.catalog, "help": self.help,
                      "wallet": lambda: self.wallet(user_id), "orders": lambda: self.orders(user_id),
                      "invoices": lambda: self.invoices(user_id)}
            if action in routes:
                return routes[action]()
        if len(parts) == 2 and action == "item":
            return self.product(parts[1])
        if len(parts) == 2 and action == "invoice":
            return self.invoice_view(self.repo.invoice(user_id, action_id(parts[1])))
        if len(parts) == 2 and action == "orders" and re.fullmatch(r"[0-9]{1,9}", parts[1]):
            return self.orders(user_id, int(parts[1]))
        if len(parts) == 3:
            if action in {"buy", "minimum"}:
                return self.checkout(user_id, parts[1], parts[2], action == "minimum")
            if action == "topup" and parts[1] in {"1000", "2000"}:
                result = self.repo.create_topup(user_id, int(parts[1]), action_id(parts[2]))
                return self.invoice_view(result["invoice"])
            if action == "event":
                return self.settle(user_id, parts[1], parts[2])
        raise DomainError("Эта кнопка устарела. Откройте меню заново.")

    def on_text(self, user_id, text, command, request_id):
        routes = {"/start": self.home, "/help": self.help, "/catalog": self.catalog,
                  "/balance": lambda: self.wallet(user_id), "/orders": lambda: self.orders(user_id),
                  "/invoices": lambda: self.invoices(user_id)}
        if command in routes:
            return routes[command]()
        if command == "/topup":
            parts = text.split()
            if len(parts) != 2 or not re.fullmatch(r"[0-9]{1,10}", parts[1]) or not request_id:
                raise DomainError("Введите сумму целым числом, например: /topup 1500.")
            return self.invoice_view(self.repo.create_topup(user_id, int(parts[1]), request_id)["invoice"])
        return View("Выберите действие в меню. Для начала нажмите /start.", self.home_rows())


class BuybackController(Controller):
    def home(self):
        offers = "\n".join(f"• {escape(o['title'])} — {money(o['amount'])}" for o in self.repo.offers())
        return View(
            "<b>Тестовая скупка</b>\n\n"
            "Отправьте сам учебный токен LAB_RAW_. Номер заказа не нужен. "
            "Проверка идёт по собственной базе этого бота.\n\n"
            f"Условные цены:\n{offers}\n\n"
            "Настоящие ключи не принимаются. Баланс учебный, выплат нет.",
            [[button("Проверить токен", "check")],
             [button("Баланс", "wallet"), button("Мои заявки", "claims")],
             [button("Примеры и тесты", "fixtures"), button("Помощь", "help")]],
        )

    def prompt(self):
        return View(
            "<b>Отправьте учебный токен</b>\n\n"
            "Одним сообщением, без кавычек и пояснений. Он должен начинаться с LAB_RAW_.\n\n"
            "Подойдёт отдельный пример из раздела «Примеры и тесты». "
            "Покупка в другом боте не требуется. Не присылайте настоящие API-ключи.",
            [[button("Примеры и тесты", "fixtures")]] + self.home_rows(),
        )

    def fixtures(self):
        state = self.repo.registry_state()
        samples = "\n\n".join(
            f"{escape(fixture['sku'])}\n<code>{escape(fixture['token'])}</code>"
            for fixture in self.repo.fixtures()[:2]
        )
        return View(
            "<b>Примеры и тесты</b>\n\n"
            f"Примеров в базе: {state['count']}. "
            f"Проверка {'включена' if state['available'] else 'приостановлена'}.\n\n"
            "Сначала добавьте примеры кнопкой ниже. Затем скопируйте один токен и отправьте сообщением.\n\n"
            f"{samples}\n\n"
            "Эта база не связана с магазином. Очистка не стирает принятые заявки и не позволяет принять токен дважды.",
            [[button("Добавить примеры в базу", "registry:seed")],
             [button("Очистить базу примеров", "registry:clear")],
             [button("Приостановить проверку" if state["available"] else "Включить проверку",
                     "registry:off" if state["available"] else "registry:on")]] + self.home_rows(),
        )

    def check(self, user_id, raw_token):
        result = self.repo.check(user_id, raw_token)
        if result["kind"] == "matched":
            return View(
                "<b>Пример найден в базе</b>\n\n"
                f"{escape(result['title'])}\nТокен: <code>{escape(result['masked_token'])}</code>\n"
                f"На учебный баланс: {money(result['amount'])}\n\n"
                "Подтвердите в течение 5 минут. Это совпадение с тестовой базой, "
                "а не проверка доступа к внешнему сервису.",
                [[button("Подтвердить тестовую сдачу", f"accept:{result['quote_id']}")],
                 [button("Отмена", "home")]],
            )
        messages = {
            "empty": "Сообщение пустое. Пришлите один учебный токен LAB_RAW_.",
            "format": "Подходит только учебный токен LAB_RAW_. Настоящие API-ключи здесь не принимаются.",
            "unavailable": "Проверка сейчас приостановлена. Токен не проверен. Попробуйте после включения проверки.",
            "notfound": "В базе этого бота такого примера нет. Заявка не создана. Добавить примеры можно в разделе «Примеры и тесты».",
            "duplicate": "Этот учебный токен уже принят. Второго начисления не будет.",
            "unsupported": "Этот вид примера сейчас не принимается. Доступные варианты есть в меню.",
        }
        return View(messages.get(result["kind"], "Не удалось завершить проверку. Попробуйте позже."),
                    [[button("Примеры и тесты", "fixtures"), button("Мои заявки", "claims")]]
                    + self.home_rows())

    def accept(self, user_id, quote_id):
        result = self.repo.accept(user_id, action_id(quote_id))
        if result["kind"] == "accepted":
            claim = result["claim"]
            text = (f"<b>Учебный токен принят</b>\n\n"
                    f"Заявка <code>{claim['id'][:8]}</code>\n"
                    f"Добавлено на учебный баланс: {money(claim['amount'])}.\n\n"
                    "Это запись для демонстрации. Денежной выплаты нет.")
        else:
            text = {
                "duplicate": "Этот токен уже принят. Баланс повторно не увеличился.",
                "unavailable": "Проверка приостановлена. Начисления нет; вернитесь после её включения.",
                "notfound": "Пример или подтверждение больше не найдены. Проверьте токен ещё раз.",
                "expired": "Подтверждение устарело или база изменилась. Отправьте учебный токен заново.",
            }.get(result["kind"], "Не удалось завершить приём. Начните с проверки токена.")
        return View(text, [[button("Баланс", "wallet"), button("Мои заявки", "claims")]]
                    + self.home_rows())

    def wallet(self, user_id):
        return View(
            f"<b>Учебный баланс</b>\n\n{money(self.repo.balance(user_id))}\n\n"
            "Это условная сумма для проверки сценария. "
            "В макете порог вывода — 500 ₽. Денежные выплаты не подключены.",
            [[button("О выплатах", "payout")]] + self.home_rows(),
        )

    def claims(self, user_id):
        claims = self.repo.claims(user_id)
        text = "<b>Последние тестовые заявки</b>\n\n" + ("\n\n".join(
            f"<code>{claim['id'][:8]}</code> · {escape(claim['title'])}\n"
            f"{escape(claim['masked_token'])} · {money(claim['amount'])} · принято"
            for claim in claims
        ) if claims else "Заявок пока нет. Начните с проверки учебного токена.")
        return View(text, [[button("Проверить токен", "check")]] + self.home_rows())

    def help(self):
        return View(
            "<b>Как проверить скупку</b>\n\n"
            "1. Откройте «Примеры и тесты».\n2. Нажмите «Добавить примеры в базу».\n"
            "3. Пришлите один из показанных токенов.\n4. Подтвердите тестовую сдачу.\n\n"
            "У бота своя база. Он не ищет заказы магазина и не проверяет API внешних сервисов. "
            "Каждый пример принимается один раз. Если ответ потерялся, откройте «Мои заявки».\n\n"
            "Присланный текст не попадает в журнал приложения. Для совпадений сохраняются только "
            "хеш и короткая маска; само сообщение остаётся в вашем чате Telegram. "
            "Поэтому настоящие ключи сюда отправлять не нужно.",
            self.home_rows(),
        )

    def on_action(self, user_id, parts):
        if len(parts) == 1:
            routes = {"home": self.home, "check": self.prompt, "fixtures": self.fixtures,
                      "wallet": lambda: self.wallet(user_id), "claims": lambda: self.claims(user_id),
                      "help": self.help,
                      "payout": lambda: View("Денежные выплаты не подключены. Заявка на вывод не создаётся.",
                                             [[button("К балансу", "wallet")]])}
            if parts[0] in routes:
                return routes[parts[0]]()
        if len(parts) == 2 and parts[0] == "accept":
            return self.accept(user_id, parts[1])
        if len(parts) == 2 and parts[0] == "registry":
            if parts[1] == "seed":
                self.repo.seed_registry()
            elif parts[1] == "clear":
                self.repo.clear_registry()
            elif parts[1] in {"on", "off"}:
                self.repo.set_available(parts[1] == "on")
            else:
                raise DomainError("Такого действия нет. Откройте меню заново.")
            return self.fixtures()
        raise DomainError("Эта кнопка устарела. Откройте меню заново.")

    def on_text(self, user_id, text, command, request_id):
        routes = {"/start": self.home, "/help": self.help, "/check": self.prompt,
                  "/balance": lambda: self.wallet(user_id), "/claims": lambda: self.claims(user_id),
                  "/fixtures": self.fixtures}
        if command in routes:
            return routes[command]()
        if command.startswith("/"):
            return View("Такой команды нет. Откройте меню: /start.", self.home_rows())
        return self.check(user_id, text)


def make_controller(config, api, repository):
    if config.kind == "store":
        return StoreController(config, api, repository)
    if config.kind == "buyback":
        return BuybackController(config, api, repository)
    raise ValueError("Unknown bot kind.")
