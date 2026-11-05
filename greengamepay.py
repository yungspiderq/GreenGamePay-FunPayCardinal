"""GreenGamePay integration for sidor0912/FunPayCardinal. Python 3.10+."""
from __future__ import annotations

import copy
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

import requests

NAME = "GreenGamePay"
VERSION = "1.0.0"
DESCRIPTION = "Steam, Telegram Stars/Premium и подарки Steam через GreenGamePay. /ggp"
CREDITS = "https://github.com/yungspiderq"
UUID = "e375b82f-b750-4dbe-bbc0-9731c39acf0a"
SETTINGS_PAGE = True
BASE_URL = "https://пополнистим.рф"
ROOT = Path("storage/plugins/greengamepay")
LOG = logging.getLogger("FPC.GreenGamePay")
DEFAULT_CONFIG = {"enabled": False, "dry_run": True, "username": "", "password": "", "lots": {}}
KINDS = {"steam", "stars", "premium", "gift"}
IN_FLIGHT = {"creating", "paying"}


class GGPError(Exception):
    """Safe error text: never includes raw provider responses or credentials."""


def number(value, label="число", allow_zero=False):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise GGPError(f"Некорректное поле: {label}.") from None
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        raise GGPError(f"Поле {label} должно быть конечным положительным числом.")
    return result


def integer(value, label="число"):
    result = number(value, label)
    if result != result.to_integral_value():
        raise GGPError(f"Поле {label} должно быть целым.")
    return int(result)


def validate_rule(rule):
    if not isinstance(rule, dict) or rule.get("kind") not in KINDS:
        raise GGPError("kind: steam, stars, premium или gift.")
    result = copy.deepcopy(rule)
    kind = result["kind"]
    result["quantity"] = str(number(result.get("quantity", 1), "quantity"))
    result["max_quantity"] = str(number(result.get("max_quantity"), "max_quantity"))
    result["max_cost"] = str(number(result.get("max_cost"), "max_cost"))
    result["currency"] = str(result.get("currency", "USD")).upper()
    if kind == "steam" and result["currency"] not in {"USD", "RUB", "KZT", "UAH"}:
        raise GGPError("Для Steam currency: USD, RUB, KZT или UAH.")
    if kind == "stars" and integer(result["quantity"]) < 50:
        raise GGPError("Минимум 50 Stars на единицу лота.")
    if kind == "premium" and integer(result["quantity"]) not in {3, 6, 12}:
        raise GGPError("Premium: quantity = 3, 6 или 12 месяцев.")
    if kind == "gift":
        result["sub_id"] = integer(result.get("sub_id"), "sub_id")
        result["region"] = str(result.get("region", "")).upper()
        if result["region"] not in {"RU", "UA", "KZ"}:
            raise GGPError("gift region: RU, UA или KZ.")
        if number(result["quantity"]) != 1:
            raise GGPError("gift quantity должна быть 1.")
    if number(result["quantity"]) > number(result["max_quantity"]):
        raise GGPError("quantity превышает max_quantity.")
    return result


def recipient(kind, text):
    value = text.strip()
    if kind == "steam" and re.fullmatch(r"[A-Za-z0-9_]{2,64}", value):
        return value
    if kind in {"stars", "premium"} and re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}", value):
        return "@" + value.lstrip("@")
    if kind == "gift":
        parsed = urlparse(value)
        if (parsed.scheme == "https" and parsed.hostname in {"s.team", "steamcommunity.com"}
                and not parsed.username and not parsed.password and not parsed.port
                and not parsed.fragment and len(value) <= 300
                and re.fullmatch(r"/[A-Za-z0-9/_-]+", parsed.path)
                and ((parsed.hostname == "s.team" and parsed.path.startswith("/p/"))
                     or parsed.path.startswith("/user/"))):
            return value
    raise GGPError("Неверный получатель. Нужен логин Steam, @username Telegram или HTTPS-ссылка приглашения Steam.")


class Config:
    def __init__(self, root):
        self.path = Path(root) / "config.json"
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = copy.deepcopy(DEFAULT_CONFIG)
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
        self.validate(self.data)
        if not self.path.exists():
            self.save(self.data)

    @staticmethod
    def validate(data):
        for key in ("enabled", "dry_run"):
            if type(data.get(key)) is not bool:
                raise GGPError(f"{key} должно быть true/false.")
        if not isinstance(data.get("lots"), dict):
            raise GGPError("lots должен быть объектом.")
        for lot, rule in data["lots"].items():
            integer(lot, "lot_id")
            validate_rule(rule)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def save(self, data):
        self.validate(data)
        with self.lock:
            temp = self.path.with_suffix(".tmp")
            with temp.open("w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(temp, 0o600)
            except OSError:
                pass
            os.replace(temp, self.path)
            self.data = copy.deepcopy(data)


class Ledger:
    """SQLite commits each phase before any provider mutation."""
    def __init__(self, root):
        self.path = Path(root) / "orders.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, state TEXT NOT NULL, body TEXT NOT NULL)")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def add(self, row):
        with self.connect() as db:
            cursor = db.execute("INSERT OR IGNORE INTO orders VALUES (?, ?, ?)",
                                (row["id"], row["state"], json.dumps(row, ensure_ascii=False)))
            return cursor.rowcount == 1

    def get(self, order_id):
        with self.connect() as db:
            row = db.execute("SELECT body FROM orders WHERE id=?", (order_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, row):
        row["updated"] = time.time()
        with self.connect() as db:
            db.execute("UPDATE orders SET state=?, body=? WHERE id=?",
                       (row["state"], json.dumps(row, ensure_ascii=False), row["id"]))

    def recent(self, limit=20):
        with self.connect() as db:
            rows = db.execute("SELECT body FROM orders ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def recover(self):
        with self.connect() as db:
            rows = db.execute("SELECT body FROM orders WHERE state IN ('creating','paying')").fetchall()
        for body, in rows:
            row = json.loads(body)
            row.update(state="review", error="Перезапуск во время запроса. Проверьте заказ у поставщика.")
            self.put(row)


class InstanceLock:
    """Prevent two Cardinal processes from spending against the same ledger."""
    def __init__(self, root):
        self.stream = (Path(root) / "instance.lock").open("a+b")
        self.stream.seek(0)
        self.stream.write(b"0")
        self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise GGPError("Это хранилище уже использует другой процесс Cardinal.") from None

    def close(self):
        self.stream.close()


class API:
    def __init__(self, config, session=None):
        self.config = config
        self.session = session or requests.Session()
        self.token = None
        self.expires = 0
        self.identity = None

    def _request(self, endpoint, payload=None, token=None, method="POST", params=None):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            response = self.session.request(method, BASE_URL + "/api/" + endpoint, json=payload,
                                            params=params, headers=headers, timeout=(10, 60),
                                            allow_redirects=False)
        except requests.RequestException:
            raise GGPError("Ошибка сети / таймаут GreenGamePay. Результат запроса неизвестен.") from None
        if response.status_code in (401, 403):
            self.token = None
            self.expires = 0
        if response.status_code != 200:
            code = ""
            try:
                body = response.json()
                candidate = body.get("error_code", "") if isinstance(body, dict) else ""
                if re.fullmatch(r"[a-z_]{1,60}", str(candidate)):
                    code = " (" + candidate + ")"
            except ValueError:
                pass
            raise GGPError(f"GreenGamePay HTTP {response.status_code}{code}; автоматического повтора нет.")
        try:
            body = response.json()
        except ValueError:
            raise GGPError("GreenGamePay вернул некорректный JSON.") from None
        if isinstance(body, dict) and (body.get("error") or body.get("detail") or body.get("success") is False):
            raise GGPError("GreenGamePay вернул ошибку в ответе HTTP 200.")
        return body

    def authenticate(self):
        cfg = self.config.snapshot()
        username = os.environ.get("GGP_USERNAME") or cfg["username"]
        password = os.environ.get("GGP_PASSWORD") or cfg["password"]
        if not username or not password:
            raise GGPError("Укажите логин и пароль GreenGamePay через /ggp_auth или GGP_USERNAME/GGP_PASSWORD.")
        identity = (username, password)
        if self.token and self.identity == identity and time.time() < self.expires - 30:
            return
        data = self._request("token", {"username": username, "password": password})
        if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not data["access_token"]:
            raise GGPError("В ответе авторизации отсутствует access_token.")
        self.token = data["access_token"]
        try:
            self.expires = float(data.get("expired", time.time() + 300))
        except (ValueError, TypeError):
            self.expires = time.time() + 300
        self.identity = identity

    def call(self, endpoint, payload=None, method="POST", params=None):
        self.authenticate()
        # No transparent retries, including 401, for mutations.
        return self._request(endpoint, payload, self.token, method, params)


class Engine:
    def __init__(self, cardinal, root=ROOT, api=None):
        self.c = cardinal
        self.config = Config(root)
        self.instance_lock = InstanceLock(root)
        self.ledger = Ledger(root)
        self.ledger.recover()
        self.api = api or API(self.config)
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="GreenGamePay")
        self.stopped = False
        self.paused = threading.Event()

    def active(self):
        plugin = self.c.plugins.get(UUID)
        return (not self.stopped and not self.paused.is_set() and bool(plugin and plugin.enabled)
                and self.config.snapshot()["enabled"])

    def close(self):
        self.stopped = True
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.instance_lock.close()

    def submit(self, func, *args):
        if self.stopped:
            return
        def safe():
            try:
                with self.lock:
                    func(*args)
            except Exception:
                # Third-party exceptions may embed URLs or credentials; never log repr/traceback.
                LOG.error("GreenGamePay: ошибка обработки; проверьте /ggp_orders.")
                self.notify("Ошибка обработки GreenGamePay. Проверьте /ggp_orders.")
        self.pool.submit(safe)

    def notify(self, text):
        if not self.c.telegram:
            return
        try:
            self.c.telegram.send_notification("<b>GreenGamePay</b>\n" + html.escape(text))
        except Exception:
            LOG.warning("GreenGamePay: уведомление Telegram не отправлено.")

    def tell(self, row, text):
        try:
            return bool(self.c.send_message(row["chat_id"], text, row["buyer_name"]))
        except Exception:
            return False

    def phase(self, row, state, **kwargs):
        row.update(state=state, **kwargs)
        self.ledger.put(row)

    def verify_order(self, row):
        order = self.c.account.get_order(row["id"])
        if (getattr(order.status, "name", "") != "PAID" or order.seller_id != self.c.account.id
                or order.buyer_id != row["buyer_id"] or str(order.chat_id) != str(row["chat_id"])
                or order.short_description != row["description"] or order.amount != row["units"]):
            raise GGPError("Заказ FunPay изменился, возвращён или не оплачен. Обработка остановлена.")
        if self.c.bl_delivery_enabled and row["buyer_name"] in self.c.blacklist:
            raise GGPError("Покупатель в чёрном списке Cardinal.")
        return order

    def new_order(self, event):
        if not self.active() or self.c.old_mode_enabled:
            return
        shortcut = event.order
        if not re.fullmatch(r"[A-Z0-9]+", shortcut.id) or shortcut.id == "ADTEST":
            return
        if self.ledger.get(shortcut.id):
            return
        lot_id = str(getattr(event, "lot_id", ""))
        rule = self.config.snapshot()["lots"].get(lot_id)
        lot = getattr(event, "lot_shortcut", None)
        if not rule or not lot:
            return
        # Native delivery runs before plugin NEW_ORDER handlers; never buy twice.
        native = getattr(event, "config_section_obj", None)
        if getattr(event, "delivered", False) or (native is not None and not native.getboolean("disable")):
            self.notify(f"#{shortcut.id}: отключите стандартную автовыдачу для лота {lot_id}.")
            return
        try:
            order = self.c.account.get_order(shortcut.id)
            # Cardinal's lot_id is inferred from text; verify against seller-authored full order fields.
            if (order.short_description != lot.description or order.buyer_id != shortcut.buyer_id
                    or order.seller_id != self.c.account.id or getattr(order.status, "name", "") != "PAID"
                    or getattr(order.subcategory, "id", None) != getattr(lot.subcategory, "id", None)):
                raise GGPError("Не удалось однозначно сопоставить оплаченный заказ и лот.")
            candidates = [item for item in self.c.profile.get_sorted_lots(1).values()
                          if item.description == order.short_description
                          and getattr(item.subcategory, "id", None) == getattr(order.subcategory, "id", None)
                          and (item.server or "") == (getattr(order.server, "name", None) or "")
                          and (item.side or "") == (getattr(order.side, "name", None) or "")]
            if len(candidates) != 1 or str(candidates[0].id) != lot_id:
                raise GGPError("Неоднозначный лот. Сделайте названия лотов уникальными в категории/сервере.")
            units = integer(order.amount, "количество FunPay")
            rule = validate_rule(rule)
            if rule["kind"] in {"premium", "gift"} and units != 1:
                raise GGPError("Premium и gift: покупайте по одной единице лота за заказ.")
            quantity = number(rule["quantity"]) * units
            if quantity > number(rule["max_quantity"]):
                raise GGPError("Количество превышает max_quantity лота.")
            row = {"id": order.id, "state": "waiting", "lot_id": lot_id, "rule": rule,
                   "quantity": str(quantity), "units": units, "description": order.short_description,
                   "buyer_id": order.buyer_id, "buyer_name": order.buyer_username,
                   "chat_id": order.chat_id, "custom_id": str(uuid.uuid4()), "created": time.time(),
                   "recipient": None, "notified": False}
            self.verify_order(row)
            if not self.ledger.add(row):
                return
            hint = {"steam": "логин Steam (не ник и не ссылку)", "stars": "@username Telegram",
                    "premium": "@username Telegram", "gift": "ссылку приглашения в друзья Steam"}[rule["kind"]]
            self.tell(row, f"Заказ #{row['id']}. Пришлите {hint} командой:\n!ggp {row['id']} ПОЛУЧАТЕЛЬ\n"
                      "Пароль от аккаунта не нужен. Затем потребуется подтверждение получателя.")
        except GGPError as exc:
            self.notify(f"#{shortcut.id}: {exc}")

    def message(self, msg):
        if not self.active() or self.c.old_mode_enabled or not getattr(msg, "text", None):
            return
        if (msg.author_id in (0, self.c.account.id) or getattr(msg, "by_bot", False)
                or getattr(msg, "badge", None)):
            return
        parts = msg.text.strip().split()
        if len(parts) < 3 or parts[0].lower() != "!ggp":
            return
        order_id = parts[1].lstrip("#").upper()
        row = self.ledger.get(order_id)
        if not row or row["buyer_id"] != msg.author_id or str(row["chat_id"]) != str(msg.chat_id):
            return
        if row["state"] not in {"waiting", "confirm"}:
            return
        try:
            self.verify_order(row)
            if (len(parts) == 4 and parts[2].lower() == "да" and row["state"] == "confirm"
                    and secrets.compare_digest(parts[3], row["nonce"])):
                if time.time() > row["confirm_until"]:
                    self.phase(row, "waiting")
                    raise GGPError("Подтверждение истекло. Отправьте получателя ещё раз.")
                self.phase(row, "ready")
                self.process(row)
            elif len(parts) == 3:
                value = recipient(row["rule"]["kind"], parts[2])
                nonce = secrets.token_hex(3)
                self.phase(row, "confirm", recipient=value, nonce=nonce, confirm_until=time.time() + 900)
                self.tell(row, f"Заказ #{row['id']}: {row['rule']['kind']}, количество {row['quantity']}, "
                          f"получатель: {value}.\nПодтвердите в течение 15 минут:\n!ggp {row['id']} да {nonce}\n"
                          "Для исправления отправьте команду с новым получателем.")
        except GGPError as exc:
            self.tell(row, str(exc))

    def check_cost(self, value, row):
        if number(value, "стоимость") > number(row["rule"]["max_cost"]):
            raise GGPError("Стоимость поставщика превышает max_cost лота.")

    def process(self, row):
        if row["state"] != "ready" or not self.active():
            return
        try:
            self.verify_order(row)
            if self.config.snapshot()["dry_run"]:
                self.phase(row, "dry_run")
                self.tell(row, f"#{row['id']}: тестовый режим, покупка у поставщика не выполнялась. Продавец уведомлён.")
                self.notify(f"DRY RUN #{row['id']}: {row['rule']['kind']} / {row['quantity']}. Списаний нет.")
                return
            rule, kind = row["rule"], row["rule"]["kind"]
            qty = number(row["quantity"])
            if kind == "steam":
                checked = self.api.call("check", {"login": row["recipient"]})
                if not isinstance(checked, dict) or checked.get("result") is not True:
                    raise GGPError("GreenGamePay не подтвердил логин Steam.")
                if rule["currency"] != "USD":
                    endpoint = "get_amount" if rule["currency"] == "RUB" else "rates"
                    payload = {"amount": float(qty)}
                    if endpoint == "rates":
                        payload["primary_currency"] = rule["currency"]
                    qty = number(self.api.call(endpoint, payload).get("usd_price"), "usd_price")
                row["provider_quantity"] = str(qty)
                self.phase(row, "creating")
                created = self.api.call("create_order", {"service_id": 1, "quantity": float(qty),
                                        "custom_id": row["custom_id"], "data": row["recipient"]})
                if not isinstance(created, dict) or created.get("custom_id") != row["custom_id"]:
                    raise GGPError("Некорректный custom_id в ответе создания.")
                self.check_cost(created.get("total"), row)
                self.phase(row, "created", provider_total=created["total"])
                self.pay_created(row, "pay_order")
            elif kind == "gift":
                quote = self.api.call("steam_gift_calculate", {"sub_id": rule["sub_id"], "region": rule["region"]})
                self.check_cost(quote.get("price"), row)
                self.phase(row, "creating")
                created = self.api.call("steam_gift_create_order", {
                    "friend_link": row["recipient"], "sub_id": rule["sub_id"], "region": rule["region"],
                    "gift_name": "FunPay " + row["id"], "gift_description": "Заказ FunPay " + row["id"]})
                provider_id = str(uuid.UUID(created.get("custom_id", "")))
                self.phase(row, "created", custom_id=provider_id, provider_total=created.get("total"))
                self.check_cost(created.get("total"), row)
                self.pay_created(row, "steam_gift_pay_order")
            else:
                amount = integer(qty)
                if kind == "stars":
                    checked = self.api.call("checkStarsRecipient", {"username": row["recipient"]})
                    if checked.get("msg") != "success" or amount < integer(checked.get("min_stars")):
                        raise GGPError("Получатель Stars не прошёл проверку или количество ниже минимума.")
                quote = self.api.call("get_stars_price" if kind == "stars" else "get_premium_price",
                                      {"quantity" if kind == "stars" else "months": amount})
                if not isinstance(quote, dict) or "ton" not in quote:
                    raise GGPError("Ответ цены не содержит ton. Покупка остановлена; нужна проверка формата API.")
                self.check_cost(quote["ton"], row)
                balance = number(self.api.call("get_ton_balance"), "баланс TON", allow_zero=True)
                if balance < number(quote["ton"]):
                    raise GGPError("Недостаточно TON на балансе поставщика.")
                self.verify_order(row)
                if not self.active():
                    return
                self.phase(row, "paying")
                result = self.api.call("buyStars" if kind == "stars" else "buyPremium",
                                       {"username": row["recipient"], "quantity" if kind == "stars" else "months": amount})
                if (not isinstance(result, dict) or result.get("msg") != "success" or not result.get("tx_id")
                        or str(result.get("username", "")).lstrip("@").lower() != row["recipient"].lstrip("@").lower()
                        or result.get("stars_amount" if kind == "stars" else "period") != amount):
                    raise GGPError("Не удалось подтвердить результат Telegram-покупки. Проверьте у поставщика.")
                self.phase(row, "paid", tx_id=str(result["tx_id"]))
            self.deliver(row)
        except Exception as exc:
            state = "review" if row["state"] in IN_FLIGHT | {"created", "paid"} else "blocked"
            error = str(exc) if isinstance(exc, GGPError) else "Ошибка обработки ответа. Требуется проверка продавцом."
            self.phase(row, state, error=error)
            self.tell(row, f"#{row['id']}: заказ передан продавцу для проверки. Повторная покупка автоматически не выполняется.")
            self.notify(f"#{row['id']} [{state}]: {error}\ncustom_id: {row['custom_id']}")

    def pay_created(self, row, endpoint):
        self.verify_order(row)
        if not self.active():
            raise GGPError("Плагин выключен до оплаты. Заказ не оплачен автоматически.")
        self.phase(row, "paying")
        result = self.api.call(endpoint, {"custom_id": row["custom_id"]})
        if (not isinstance(result, dict) or result.get("custom_id") != row["custom_id"]
                or result.get("msg") != "Successfully paid the order"):
            raise GGPError("Ответ не подтверждает оплату. Проверьте заказ у поставщика.")
        self.phase(row, "paid")

    def deliver(self, row):
        if row["state"] != "paid":
            return
        text = (f"#{row['id']}: поставщик подтвердил оплату {row['rule']['kind']} для {row['recipient']}. "
                "Проверьте поступление. Подтверждайте заказ FunPay только после получения товара.")
        if self.tell(row, text):
            self.phase(row, "done", notified=True)
        self.notify(f"#{row['id']}: оплата подтверждена; сообщение покупателю "
                    + ("отправлено." if row.get("notified") else "не доставлено, /ggp_redeliver " + row["id"]))

    def status_changed(self, event):
        row = self.ledger.get(event.order.id)
        if row and getattr(event.order.status, "name", "") != "PAID" and row["state"] in {"waiting", "confirm", "ready", "blocked"}:
            self.phase(row, "cancelled", error="Статус заказа FunPay изменён.")


HELP = """GreenGamePay — /ggp
/ggp_auth ЛОГИН ПАРОЛЬ — вход на пополнистим.рф
/ggp_balance — USD и TON; /ggp_services — каталог
/ggp_lots — лоты FunPay; /ggp_rules — привязки
/ggp_bind LOT_ID JSON — настроить лот
/ggp_unbind LOT_ID — удалить привязку для новых заказов
/ggp_on /ggp_off — включить / остановить
/ggp_dry on|off — тестовый режим
/ggp_orders — последние заказы
/ggp_order ORDER_ID — состояние заказа
/ggp_resume ORDER_ID — повторить только ДО создания/оплаты (blocked/ready)
/ggp_redeliver ORDER_ID — повторить сообщение об оплате без покупки
/ggp_resolve ORDER_ID done|cancelled — закрыть после ручной проверки (не покупает)

Пример: /ggp_bind 12345 {"kind":"steam","quantity":"1","currency":"USD","max_quantity":"10","max_cost":"10"}
max_cost: USD для Steam/gift, TON для Stars/Premium. Для Telegram это лимит котировки без гарантии финальной комиссии.
Количество = quantity × число единиц FunPay. Для Premium/gift допускается одна единица.
Перед включением отключите стандартную автовыдачу для этих лотов и старый режим сообщений Cardinal.
"""


def authorized(c, obj):
    return bool(c.telegram and obj.from_user and obj.from_user.id in c.telegram.authorized_users
                and obj.chat.type == "private")


def telegram_command(engine, message):
    c = engine.c
    if not authorized(c, message):
        return
    bot = c.telegram.bot
    command, _, argument = (message.text or "").partition(" ")
    command = command.split("@")[0].lower()
    argument = argument.strip()

    def reply(text):
        # Escape every chunk independently to avoid breaking HTML entities.
        for start in range(0, len(text), 3000):
            bot.send_message(message.chat.id, html.escape(text[start:start + 3000]))

    try:
        with engine.lock:
            cfg = engine.config.snapshot()
            if command == "/ggp":
                reply(f"GreenGamePay {VERSION}\nВключён: {cfg['enabled']} | Тест: {cfg['dry_run']}\n" + HELP)
            elif command == "/ggp_auth":
                login, sep, password = argument.partition(" ")
                if not sep or not login or not password:
                    raise GGPError("Формат: /ggp_auth ЛОГИН ПАРОЛЬ")
                cfg.update(username=login, password=password)
                engine.config.save(cfg)
                try:
                    bot.delete_message(message.chat.id, message.id)
                except Exception:
                    pass
                reply("Данные сохранены локально. Проверьте подключение: /ggp_balance. Переменные окружения имеют приоритет.")
            elif command in {"/ggp_on", "/ggp_off", "/ggp_dry"}:
                if command == "/ggp_dry":
                    if argument not in {"on", "off"}:
                        raise GGPError("Формат: /ggp_dry on|off")
                    cfg["dry_run"] = argument == "on"
                else:
                    if command == "/ggp_on" and c.old_mode_enabled:
                        raise GGPError("Отключите oldMsgGetMode в настройках Cardinal.")
                    cfg["enabled"] = command == "/ggp_on"
                engine.config.save(cfg)
                if command == "/ggp_on":
                    engine.paused.clear()
                reply(f"Включён: {cfg['enabled']}; тестовый режим: {cfg['dry_run']}.")
            elif command == "/ggp_bind":
                lot_id, _, body = argument.partition(" ")
                integer(lot_id, "LOT_ID")
                rule = validate_rule(json.loads(body))
                lots = c.profile.get_sorted_lots(1)
                if int(lot_id) not in lots:
                    raise GGPError("Лот не найден в профиле продавца. Проверьте /ggp_lots.")
                cfg["lots"][str(int(lot_id))] = rule
                engine.config.save(cfg)
                reply("Привязка сохранена. Отключите стандартную автовыдачу для этого лота. /ggp_rules")
            elif command == "/ggp_unbind":
                cfg["lots"].pop(str(integer(argument, "LOT_ID")), None)
                engine.config.save(cfg)
                reply("Привязка удалена для новых заказов. Существующие используют сохранённые настройки.")
            elif command == "/ggp_rules":
                reply(json.dumps(cfg["lots"], ensure_ascii=False, indent=2))
            elif command == "/ggp_lots":
                reply("\n".join(f"{key}: {lot.description}" for key, lot in c.profile.get_sorted_lots(1).items()) or "Лотов нет.")
            elif command == "/ggp_balance":
                reply("USD: " + str(engine.api.call("check_balance")) + "\nTON: " + str(engine.api.call("get_ton_balance")))
            elif command == "/ggp_services":
                data = engine.api.call("get_all_services")
                lines = []
                for category in data.get("categories", []):
                    for item in category.get("services", []):
                        lines.append(f"{item.get('service_id')}: {item.get('service_name')} | "
                                     f"{item.get('price')} {item.get('currency')} | остаток {item.get('in_stock')}")
                reply("\n".join(lines) or "Каталог пуст.")
            elif command == "/ggp_orders":
                reply("\n".join(f"#{r['id']}: {r['state']} / {r['rule']['kind']} / {r['quantity']}"
                                for r in engine.ledger.recent()) or "Заказов нет.")
            elif command in {"/ggp_order", "/ggp_resume", "/ggp_redeliver", "/ggp_resolve"}:
                parts = argument.split()
                row = engine.ledger.get(parts[0].lstrip("#").upper()) if parts else None
                if not row:
                    raise GGPError("Заказ не найден.")
                if command == "/ggp_order":
                    reply(json.dumps(row, ensure_ascii=False, indent=2))
                elif command == "/ggp_resume":
                    if row["state"] not in {"blocked", "ready"} or not engine.active():
                        raise GGPError("Доступно только для blocked/ready при включённом плагине. review повторять нельзя.")
                    engine.phase(row, "ready")
                    engine.process(row)
                    reply("Состояние: " + engine.ledger.get(row["id"])["state"])
                elif command == "/ggp_redeliver":
                    if row["state"] not in {"paid", "done"}:
                        raise GGPError("Нет подтверждённой оплаты.")
                    engine.phase(row, "paid")
                    engine.deliver(row)
                    reply("Сообщение обработано. Покупка не повторялась.")
                else:
                    if len(parts) != 2 or parts[1] not in {"done", "cancelled"} or row["state"] in IN_FLIGHT:
                        raise GGPError("Формат: /ggp_resolve ORDER_ID done|cancelled; только после ручной проверки.")
                    engine.phase(row, parts[1], manual_resolution=True, resolved_by=message.from_user.id)
                    reply("Отметка сохранена. Запросов покупки и возврата не было.")
    except (GGPError, ValueError, TypeError, KeyError) as exc:
        reply(str(exc) if isinstance(exc, GGPError) else "Некорректные параметры или формат ответа. Проверьте инструкцию.")


_engine = None


def init(cardinal, *args):
    global _engine
    if _engine is not None:
        return
    try:
        _engine = Engine(cardinal)
    except Exception:
        LOG.error("GreenGamePay: ошибка конфигурации/хранилища. Плагин не запущен; данные не сбрасывались.")
        return
    if cardinal.telegram:
        commands = ["ggp", "ggp_auth", "ggp_balance", "ggp_services", "ggp_lots", "ggp_rules", "ggp_bind",
                    "ggp_unbind", "ggp_on", "ggp_off", "ggp_dry", "ggp_orders", "ggp_order", "ggp_resume",
                    "ggp_redeliver", "ggp_resolve"]
        cardinal.add_telegram_commands(UUID, [("ggp", "GreenGamePay: управление", True)])

        def command(message):
            if authorized(cardinal, message):
                # Stop new payment stages immediately, even while another API request is running.
                if (message.text or "").split()[0].split("@")[0].lower() == "/ggp_off":
                    _engine.paused.set()
                _engine.submit(telegram_command, _engine, message)

        cardinal.telegram.msg_handler(command, commands=commands)
        from tg_bot import CBT

        def settings(call):
            if (call.from_user.id not in cardinal.telegram.authorized_users
                    or not call.message or call.message.chat.type != "private"):
                return
            cardinal.telegram.bot.answer_callback_query(call.id)
            cardinal.telegram.bot.send_message(call.message.chat.id, html.escape(HELP))

        cardinal.telegram.cbq_handler(settings, lambda call: call.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}:"))
    LOG.info("GreenGamePay %s initialized", VERSION)


def new_order(cardinal, event, *args):
    if _engine:
        _engine.submit(_engine.new_order, event)


def new_message(cardinal, event, *args):
    if _engine:
        _engine.submit(_engine.message, event.message)


def status_changed(cardinal, event, *args):
    if _engine:
        _engine.submit(_engine.status_changed, event)


def stop(*args):
    if _engine:
        _engine.stopped = True
        _engine.pool.shutdown(wait=False, cancel_futures=True)
        # Keep the instance lock until in-flight work has actually finished.
        threading.Thread(target=_engine.close, name="GGP-shutdown", daemon=True).start()


BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_ORDER = [new_order]
BIND_TO_NEW_MESSAGE = [new_message]
BIND_TO_ORDER_STATUS_CHANGED = [status_changed]
BIND_TO_PRE_STOP = [stop]
BIND_TO_DELETE = stop
