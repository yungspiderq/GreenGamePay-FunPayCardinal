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
VERSION = "1.2.0"
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
    if type(result.get("enabled", True)) is not bool:
        raise GGPError("enabled для лота должно быть true/false.")
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


def order_quantity(rule, units):
    """Shared by the preview calculator and real order processing."""
    units = integer(units, "число штук лота")
    if rule["kind"] in {"premium", "gift"} and units != 1:
        raise GGPError("Для подписки Premium или подарка Steam покупайте по одной штуке за заказ.")
    quantity = number(rule["quantity"]) * units
    if quantity > number(rule["max_quantity"]):
        raise GGPError("Объём покупки превышает настроенный максимум одного заказа.")
    return quantity


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

    def summary(self, now=None):
        cutoff = (time.time() if now is None else now) - 86400
        result = {"total": 0, "last24": 0, "waiting": 0, "attention": 0, "paid": 0,
                  "manual": 0, "dry_run": 0, "cancelled": 0, "processing": 0}
        with self.connect() as db:
            for body, in db.execute("SELECT body FROM orders"):
                row = json.loads(body)
                result["total"] += 1
                result["last24"] += row.get("created", 0) >= cutoff
                state = row["state"]
                if state in {"waiting", "confirm"}:
                    result["waiting"] += 1
                elif state in {"blocked", "review", "created", "ready"}:
                    result["attention"] += 1
                elif state in {"paid", "done"}:
                    result["manual" if row.get("manual_resolution") else "paid"] += 1
                elif state in {"dry_run", "cancelled"}:
                    result[state] += 1
                else:
                    result["processing"] += 1
        return result

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
        self.panel = Panel(self)

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
        if not rule or not lot or not rule.get("enabled", True):
            return
        # Native delivery runs before plugin NEW_ORDER handlers; never buy twice.
        native = getattr(event, "config_section_obj", None)
        if getattr(event, "delivered", False) or (native is not None and not native.getboolean("disable")):
            self.notify(f"#{shortcut.id}: отключите стандартную автовыдачу для лота {lot_id}.")
            return
        try:
            order = self.c.account.get_order(shortcut.id)
            if getattr(lot, "auto", False) or getattr(order, "order_secrets", None):
                raise GGPError("У лота включена выдача самого FunPay. Отключите её перед подключением GreenGamePay.")
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
            quantity = order_quantity(rule, units)
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
                self.tell(row, f"Заказ #{row['id']}: {KIND_NAMES[row['rule']['kind']]}, "
                          f"получите {row['quantity']} {quantity_unit(row['rule'])}, "
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
        text = (f"#{row['id']}: поставщик подтвердил оплату. Услуга: {KIND_NAMES[row['rule']['kind']]}. "
                f"Получатель: {row['recipient']}. "
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
                engine.panel.open(message)
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
                    if row["state"] not in {"paid", "done"} or row.get("manual_resolution"):
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


KIND_NAMES = {"steam": "🎮 Пополнение Steam", "stars": "⭐ Telegram Stars",
              "premium": "💎 Telegram Premium", "gift": "🎁 Подарок Steam"}
STATE_NAMES = {"waiting": "Ждём получателя", "confirm": "Ждём подтверждения", "ready": "Готов к обработке",
               "blocked": "Нужно исправить ошибку", "creating": "Создаём заказ", "created": "Создан у поставщика",
               "paying": "Оплачиваем", "review": "Нужна ручная проверка", "paid": "Оплачен, сообщение не доставлено",
               "done": "Обработка завершена", "dry_run": "Тест без покупки", "cancelled": "Обработка закрыта"}


def quantity_unit(rule):
    return (rule.get("currency", "USD") if rule["kind"] == "steam"
            else {"stars": "Stars", "premium": "мес.", "gift": "игра"}[rule["kind"]])


def quantity_example(rule):
    unit, qty = quantity_unit(rule), number(rule["quantity"])
    if rule["kind"] in {"premium", "gift"}:
        return f"Один заказ: 1 штука лота → {qty} {unit}."
    return (f"Покупатель выбрал 1 шт. → получит {qty} {unit}.\n"
            f"Покупатель выбрал 2 шт. → получит {qty * 2} {unit}.\n"
            "Если общий объём выше максимума заказа, покупка остановится.")


def rule_text(rule):
    kind = rule["kind"]
    unit = quantity_unit(rule)
    cost_unit = "USD" if kind in {"steam", "gift"} else "TON"
    label = "Сумма пополнения за 1 штуку лота" if kind == "steam" else "Покупатель получает за 1 штуку лота"
    text = (f"{KIND_NAMES[kind]}\n{label}: {rule['quantity']} {unit}\n"
            f"Максимум за заказ: {rule['max_quantity']} {unit}\n"
            f"Лимит {'котировки' if cost_unit == 'TON' else 'стоимости'}: {rule['max_cost']} {cost_unit}")
    if kind == "gift":
        text += f"\nПакет Steam (SUB_ID): {rule['sub_id']} · Регион: {rule['region']}"
    return text + "\n\n" + quantity_example(rule)


class Panel:
    """One active inline panel and one expiring wizard per private administrator chat."""
    def __init__(self, engine):
        self.e = engine
        self.sessions = {}
        self.pending = {}

    @staticmethod
    def key(ctx):
        return ctx["chat"], ctx["user"]

    @staticmethod
    def context(message, user=None, edit=False):
        return {"chat": message.chat.id, "user": user or message.from_user.id,
                "mid": message.id if edit else None}

    def clear(self, ctx):
        self.pending.pop(self.key(ctx), None)
        tg = self.e.c.telegram
        if hasattr(tg, "clear_state"):
            state = tg.get_state(ctx["chat"], ctx["user"])
            if state and state.get("state") == "ggp_input":
                tg.clear_state(ctx["chat"], ctx["user"])

    def show(self, ctx, text, rows):
        from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
        token = secrets.token_hex(4)
        keyboard = InlineKeyboardMarkup()
        for row in rows:
            buttons = []
            for title, action in row:
                data = action[1:] if action.startswith("=") else f"ggp:{token}:{action}"
                if len(data.encode("utf8")) > 64:
                    raise GGPError("Слишком длинный идентификатор кнопки.")
                buttons.append(InlineKeyboardButton(title, callback_data=data))
            keyboard.row(*buttons)
        title, _, body = text.partition("\n")
        rendered = "<b>" + html.escape(title) + "</b>\n" + html.escape(body)
        bot = self.e.c.telegram.bot
        if ctx.get("mid"):
            try:
                bot.edit_message_text(rendered, ctx["chat"], ctx["mid"], reply_markup=keyboard)
            except Exception as exc:
                if "message is not modified" not in str(exc).lower():
                    sent = bot.send_message(ctx["chat"], rendered, reply_markup=keyboard)
                    ctx["mid"] = sent.message_id
        else:
            sent = bot.send_message(ctx["chat"], rendered, reply_markup=keyboard)
            ctx["mid"] = sent.message_id
        self.sessions[self.key(ctx)] = {"token": token, "mid": ctx["mid"]}

    def home(self, ctx, note=""):
        from tg_bot import CBT
        self.clear(ctx)
        cfg = self.e.config.snapshot()
        connected = bool((os.environ.get("GGP_USERNAME") or cfg["username"])
                         and (os.environ.get("GGP_PASSWORD") or cfg["password"]))
        enabled = cfg["enabled"] and not self.e.paused.is_set()
        text = (f"🌿 GreenGamePay v{VERSION}\n\n"
                f"Автопокупка: {'ВКЛ 🟢' if enabled else 'ВЫКЛ 🔴'}\n"
                f"Режим: {'🧪 Тест — деньги не списываются' if cfg['dry_run'] else '💳 Реальные покупки'}\n"
                f"Подключение: {'данные указаны' if connected else 'не настроено'}\n"
                f"Настроено лотов: {len(cfg['lots'])}, на паузе: {sum(not r.get('enabled', True) for r in cfg['lots'].values())}\n\n"
                "Как начать:\n1. Подключите GreenGamePay.\n2. Выберите лоты и настройте товар.\n"
                "3. Включите автопокупку. Сначала проверьте тестовый режим.\n\n"
                "Покупатель указывает получателя и подтверждает его в чате FunPay.")
        plugin = self.e.c.plugins.get(UUID)
        if not plugin or not plugin.enabled:
            text += "\n\n⏸ Плагин отключён в Cardinal. Включите его в карточке плагина."
        if note:
            text += "\n\n" + note
        self.show(ctx, text, [
            [("🔑 Подключение", "connect"), ("💰 Баланс", "balance")],
            [("📦 Мои лоты", "lots:0"), ("📋 Заказы", "orders:0")],
            [("🩺 Проверка готовности", "diagnostics"), ("📊 Сводка", "summary")],
            [("🟢 Включить автопокупку" if not enabled else "🔴 Остановить автопокупку", "enable" if not enabled else "disable")],
            [("✅ 🧪 Тестовый режим" if cfg["dry_run"] else "⬜ 🧪 Тестовый режим", "live" if cfg["dry_run"] else "test")],
            [("📚 Каталог поставщика", "catalog:0")],
            [("❓ Как это работает", "help")],
            [("🔄 Обновить меню", "home")],
            [("⬅️ Назад к плагину", f"={CBT.EDIT_PLUGIN}:{UUID}:0")]])

    def open(self, message):
        if authorized(self.e.c, message):
            self.home(self.context(message))

    def lots(self, ctx, page=0):
        self.clear(ctx)
        cfg = self.e.config.snapshot()
        items = list(self.e.c.profile.get_sorted_lots(1).items())
        # Include removed lots so stale rules can still be inspected and unbound.
        known = {str(key) for key, _ in items}
        items += [(int(key), None) for key in cfg["lots"] if key not in known]
        page = max(0, min(page, max(0, (len(items) - 1) // 6)))
        rows = []
        for lot_id, lot in items[page * 6:page * 6 + 6]:
            rule = cfg["lots"].get(str(lot_id))
            label = ("⏸ " if rule and not rule.get("enabled", True) else "✅ " if rule else "⬜ ") + f"#{lot_id} · "
            label += (lot.description or "Без названия")[:34] if lot else "Лот удалён с FunPay"
            rows.append([(label, f"lot:{lot_id}")])
        self.pager(rows, "lots", page, len(items), 6)
        rows.append([("⬅️ В главное меню", "home")])
        self.show(ctx, "📦 Мои лоты\n\nВыберите лот для настройки.\n✅ — настроен; ⏸ — пауза; ⬜ — не настроен.\n"
                  "Настройки применяются только к новым заказам.\n\n"
                  + (f"Страница {page + 1}" if items else "Лотов пока нет. Добавьте лот на FunPay и обновите профиль Cardinal."), rows)

    @staticmethod
    def pager(rows, name, page, count, size):
        nav = []
        if page:
            nav.append(("⬅️", f"{name}:{page - 1}"))
        if (page + 1) * size < count:
            nav.append(("➡️", f"{name}:{page + 1}"))
        if nav:
            rows.append(nav)

    def lot(self, ctx, lot_id):
        self.clear(ctx)
        rule = self.e.config.snapshot()["lots"].get(lot_id)
        lot = self.e.c.profile.get_sorted_lots(1).get(int(lot_id))
        text = f"📦 Лот #{lot_id}\n\n" + ((lot.description or "Без названия")[:700] if lot else "Лот больше не найден на FunPay.")
        text += "\n\n" + (rule_text(rule) if rule else "Этот лот пока не подключён к GreenGamePay.")
        if rule and not rule.get("enabled", True):
            text += "\n\n⏸ Новые заказы этого лота не обрабатываются. Уже принятые продолжают выполняться."
        text += "\n\nОтключите для него стандартную автовыдачу Cardinal и другие плагины выдачи."
        rows = []
        if lot:
            rows.append([("⚙️ Изменить настройки" if rule else "➕ Настроить лот", f"setup:{lot_id}")])
        if rule:
            rows.append([("🧮 Проверить расчёт", f"calculate:{lot_id}")])
            rows.append([("⏸ Пауза для новых заказов" if rule.get("enabled", True) else "▶️ Принимать новые заказы",
                          f"lotpause:{lot_id}" if rule.get("enabled", True) else f"lotresume:{lot_id}")])
            rows.append([("🗑 Отключить этот лот", f"unask:{lot_id}")])
        rows += [[("⬅️ К списку лотов", "lots:0")]]
        self.show(ctx, text, rows)

    def begin(self, ctx, step, **data):
        tg = self.e.c.telegram
        if hasattr(tg, "set_state"):
            tg.set_state(ctx["chat"], ctx.get("mid") or 0, ctx["user"], "ggp_input")
        self.pending[self.key(ctx)] = {"step": step, "expires": time.time() + 900, "data": data}
        self.prompt(ctx)

    def prompt(self, ctx, error=""):
        state = self.pending[self.key(ctx)]
        step, data = state["step"], state["data"]
        rule = data.get("rule", {})
        rows = []
        prompts = {
            "login": "🔑 Подключение · шаг 1 из 2\n\nОтправьте логин вашего аккаунта GreenGamePay (пополнистим.рф).",
            "password": "🔑 Подключение · шаг 2 из 2\n\nОтправьте пароль GreenGamePay отдельным сообщением.\n"
                        "Бот попробует удалить это сообщение. Данные сохраняются только на сервере Cardinal.",
            "kind": "📦 Настройка лота\n\nКакой товар получает покупатель?",
            "currency": "🎮 Валюта пополнения Steam\n\nВ какой валюте указан номинал вашего лота?\nЭто валюта пополнения, а не цена продажи на FunPay.",
            "quantity": "📏 Что получает покупатель\n\nПокупатель выбирает в заказе FunPay количество: 1, 2, 3 штуки.\n"
                        "Сколько товара нужно выдать за 1 штуку?\n",
            "max_quantity": "📏 Максимум за заказ\n\nУкажите максимальный общий объём одного заказа.\n"
                            "Например: 50 Stars за единицу × 4 единицы = 200 Stars. Для такого заказа максимум должен быть не меньше 200.",
            "max_cost": "💰 Лимит стоимости\n\n",
            "sub_id": "🎁 Пакет игры Steam\n\nОтправьте SUB_ID пакета игры. Это ID пакета, а не APP_ID приложения.",
            "region": "🌍 Регион получателя\n\nДля какого региона аккаунта продаётся подарок?",
            "calculate": "🧮 Калькулятор заказа\n\n" + (quantity_example(rule) if step == "calculate" else "")
                         + "\n\nВведите число штук, которое покупатель выбрал на FunPay. Например: 2.\n"
                         "Это только расчёт: покупка не создаётся и деньги не списываются.",
            "findorder": "🔎 Поиск заказа\n\nОтправьте номер заказа FunPay, например AB123 или #AB123.",
            "review": "✅ Проверьте настройки\n\n" + (rule_text(rule) if step == "review" else "") + "\n\nСохранить для новых заказов этого лота?"}
        text = prompts[step]
        if step == "kind":
            rows = [[(title, "kind:" + kind)] for kind, title in KIND_NAMES.items()]
        elif step == "currency":
            rows = [[(value, "currency:" + value) for value in ("USD", "RUB")],
                    [(value, "currency:" + value) for value in ("KZT", "UAH")]]
        elif step == "region":
            rows = [[(value, "region:" + value) for value in ("RU", "UA", "KZ")]]
        elif step == "quantity":
            kind = rule["kind"]
            if kind == "premium":
                text += "Выберите длительность подписки. Один заказ FunPay — одна подписка."
                rows = [[(value + " мес.", "months:" + value) for value in ("3", "6", "12")]]
            else:
                currency = rule.get("currency", "USD")
                text += (f"\nВведите сумму, которую покупатель получит на Steam, в {currency}.\n"
                         f"Пример: вводите 100 → за 1 шт. начисляем 100 {currency}, за 2 шт. — 200 {currency}.\n"
                         "Цену продажи вы отдельно задаёте на FunPay."
                         if kind == "steam" else "\nВведите число Stars за 1 штуку лота, минимум 50.\n"
                         "Пример: вводите 50 → за 1 шт. выдаём 50 Stars, за 2 шт. — 100 Stars.")
        elif step == "max_quantity":
            text = ("📏 Максимум за один заказ\n\n" + quantity_example(rule) + "\n\n"
                    f"Введите максимальный общий объём в {quantity_unit(rule)}.\n"
                    f"Он должен быть не меньше {rule['quantity']} {quantity_unit(rule)}.\n"
                    "Это ограничение объёма товара, а не цена продажи.")
        elif step == "max_cost":
            text += ("Укажите максимум в USD на весь заказ. Например: 10. Проверяем стоимость до оплаты."
                     if rule["kind"] in {"steam", "gift"} else
                     "Укажите максимум котировки в TON на весь заказ. Например: 5.\n"
                     "Окончательное списание может быть выше из-за комиссии или изменения курса.")
        elif step == "review":
            rows = [[("💾 Сохранить настройки", "save")]]
        if error:
            text += "\n\n⚠️ " + error
        rows += [[("✖️ Отмена", "home")]]
        self.show(ctx, text, rows)

    def next_step(self, ctx, step):
        self.pending[self.key(ctx)]["step"] = step
        self.pending[self.key(ctx)]["expires"] = time.time() + 900
        self.prompt(ctx)

    def has_input(self, message):
        if not authorized(self.e.c, message) or not getattr(message, "text", None) or message.text.startswith("/"):
            return False
        key = message.chat.id, message.from_user.id
        if key not in self.pending:
            return False
        tg = self.e.c.telegram
        if hasattr(tg, "get_state"):
            state = tg.get_state(*key)
            if not state or state.get("state") != "ggp_input":
                self.pending.pop(key, None)
                return False
        return True

    def input(self, message):
        if not self.has_input(message):
            return
        ctx = self.context(message)
        ctx["mid"] = self.sessions.get(self.key(ctx), {}).get("mid")
        state = self.pending[self.key(ctx)]
        secret_step = state["step"] == "password"
        deleted = False
        if secret_step:
            try:
                self.e.c.telegram.bot.delete_message(message.chat.id, message.id)
                deleted = True
            except Exception:
                pass
        if time.time() > state["expires"]:
            self.home(ctx, "⌛ Время ввода истекло. Начните настройку заново.")
            return
        text, data, step = message.text.strip(), state["data"], state["step"]
        try:
            if step == "calculate":
                units = integer(text, "число штук лота")
                rule = data["rule"]
                total = order_quantity(rule, units)
                self.clear(ctx)
                self.show(ctx, "🧮 Расчёт заказа\n\n"
                          f"Покупатель выбрал: {units} шт.\n"
                          f"За 1 шт. получает: {rule['quantity']} {quantity_unit(rule)}\n"
                          f"Итого получит: {total} {quantity_unit(rule)}\n\n"
                          "✅ Общий объём укладывается в лимит.\n"
                          "Расчёт не проверяет остатки, курс или баланс поставщика. Покупка не создавалась.",
                          [[("🧮 Другой расчёт", "calculate:" + data["lot_id"])],
                           [("⬅️ К лоту", "lot:" + data["lot_id"])]])
            elif step == "findorder":
                order_id = text.lstrip("#").upper()
                if not re.fullmatch(r"[A-Z0-9]{1,32}", order_id) or not self.e.ledger.get(order_id):
                    raise GGPError("Заказ не найден в журнале плагина. Проверьте номер и отправьте его ещё раз.")
                self.order(ctx, order_id)
            elif step == "login":
                if not text or len(text) > 200 or "\n" in text:
                    raise GGPError("Отправьте логин одной строкой.")
                data["login"] = text
                self.next_step(ctx, "password")
            elif step == "password":
                if not message.text or len(message.text) > 1000:
                    raise GGPError("Пароль должен содержать от 1 до 1000 символов.")
                cfg = self.e.config.snapshot()
                cfg.update(username=data["login"], password=message.text)
                self.e.config.save(cfg)
                note = "✅ Данные сохранены. Нажмите «Баланс», чтобы проверить подключение."
                if not deleted:
                    note += "\nСообщение с паролем не удалось удалить — удалите его вручную."
                self.home(ctx, note)
            elif step in {"quantity", "max_quantity", "max_cost", "sub_id"}:
                rule = data["rule"]
                value = str(number(text.replace(",", "."), "значение"))
                if step == "quantity" and rule["kind"] == "premium":
                    raise GGPError("Выберите 3, 6 или 12 месяцев кнопкой ниже.")
                if step == "quantity" and rule["kind"] == "stars" and integer(value) < 50:
                    raise GGPError("Минимум 50 Stars за единицу.")
                if step == "max_quantity" and number(value) < number(rule["quantity"]):
                    raise GGPError("Максимум не может быть меньше объёма за одну единицу.")
                rule[step] = integer(value, "SUB_ID") if step == "sub_id" else value
                next_step = {"quantity": "max_quantity", "max_quantity": "max_cost", "sub_id": "region", "max_cost": "review"}[step]
                if next_step == "review":
                    data["rule"] = validate_rule(rule)
                self.next_step(ctx, next_step)
            else:
                raise GGPError("На этом шаге выберите вариант кнопкой ниже.")
        except GGPError as exc:
            self.prompt(ctx, str(exc))

    def wizard_action(self, ctx, action, arg):
        state = self.pending.get(self.key(ctx))
        required = {"kind": "kind", "currency": "currency", "months": "quantity", "region": "region", "save": "review"}
        if not state or time.time() > state["expires"] or state["step"] != required[action]:
            self.home(ctx, "⌛ Этот шаг уже закрыт. Начните настройку лота заново.")
            return
        data = state["data"]
        rule = data["rule"]
        if action == "kind" and arg in KINDS:
            rule["kind"] = arg
            if arg == "gift":
                rule.update(quantity="1", max_quantity="1")
                self.next_step(ctx, "sub_id")
            else:
                self.next_step(ctx, "currency" if arg == "steam" else "quantity")
        elif action == "currency" and arg in {"USD", "RUB", "KZT", "UAH"}:
            rule["currency"] = arg
            self.next_step(ctx, "quantity")
        elif action == "months" and rule["kind"] == "premium" and arg in {"3", "6", "12"}:
            rule.update(quantity=arg, max_quantity=arg)
            self.next_step(ctx, "max_cost")
        elif action == "region" and arg in {"RU", "UA", "KZ"}:
            rule["region"] = arg
            self.next_step(ctx, "max_cost")
        elif action == "save":
            cfg = self.e.config.snapshot()
            if int(data["lot_id"]) not in self.e.c.profile.get_sorted_lots(1):
                raise GGPError("Лот больше не найден на FunPay. Обновите профиль Cardinal.")
            cfg["lots"][data["lot_id"]] = validate_rule(rule)
            self.e.config.save(cfg)
            self.lot(ctx, data["lot_id"])

    def orders(self, ctx, page=0):
        self.clear(ctx)
        orders = self.e.ledger.recent(100)
        page = max(0, min(page, max(0, (len(orders) - 1) // 6)))
        rows = [[(f"#{row['id']} · {STATE_NAMES.get(row['state'], row['state'])}", "order:" + row["id"])]
                for row in orders[page * 6:page * 6 + 6]]
        self.pager(rows, "orders", page, len(orders), 6)
        rows += [[("🔎 Найти по номеру", "findorder")],
                 [("🔄 Обновить список", f"orders:{page}")], [("⬅️ В главное меню", "home")]]
        self.show(ctx, "📋 Заказы\n\n" + ("Выберите заказ, чтобы посмотреть статус и доступные действия.\n"
                  "Журнал сохраняется после перезапуска." if orders else "Заказов ещё нет. Здесь появятся покупки настроенных лотов."), rows)

    def diagnostics(self, ctx):
        self.clear(ctx)
        cfg, c = self.e.config.snapshot(), self.e.c
        plugin = c.plugins.get(UUID)
        credentials = bool((os.environ.get("GGP_USERNAME") or cfg["username"])
                           and (os.environ.get("GGP_PASSWORD") or cfg["password"]))
        lines = [("✅" if plugin and plugin.enabled else "⚠️") + " Плагин в Cardinal: "
                 + ("включён" if plugin and plugin.enabled else "включите в карточке плагина"),
                 ("⚠️ Отключите старый режим сообщений Cardinal" if c.old_mode_enabled else "✅ Новый режим сообщений"),
                 ("✅ Данные GreenGamePay указаны" if credentials else "⚠️ Введите данные GreenGamePay в «Подключении»"),
                 ("🧪 Сейчас тестовый режим, покупок не будет" if cfg["dry_run"] else "💳 Сейчас режим реальных покупок"),
                 ("🟢 Автопокупка включена" if self.e.active() else "⏸ Автопокупка остановлена")]
        lots = c.profile.get_sorted_lots(1)
        enabled = {key: rule for key, rule in cfg["lots"].items() if rule.get("enabled", True)}
        issues = []
        if not enabled:
            issues.append("Нет подключённых лотов без паузы. Настройте или возобновите лот.")
        native_cfg = getattr(c, "AD_CFG", {})
        def signature(item):
            return (item.description, getattr(item.subcategory, "id", None), item.server or "", item.side or "")

        for key in enabled:
            lot = lots.get(int(key))
            if not lot:
                issues.append(f"#{key}: лот не найден в профиле Cardinal.")
                continue
            if sum(signature(item) == signature(lot) for item in lots.values()) != 1:
                issues.append(f"#{key}: совпадающие названия лотов. Сделайте название уникальным.")
            if getattr(lot, "auto", False):
                issues.append(f"#{key}: отключите автовыдачу самого FunPay.")
            description = ", ".join(str(part) for part in (lot.server, lot.side, lot.description) if part)
            for match in (lambda name: name == description, lambda name: description.startswith(name),
                          lambda name: name in description):
                names = [name for name in native_cfg if name and name != "DEFAULT" and match(name)]
                if not names:
                    continue
                section = native_cfg[max(names, key=len)]
                disabled = (section.getboolean("disable") if hasattr(section, "getboolean")
                            else str(section.get("disable", False)).lower() in {"1", "true", "yes", "on"})
                if not disabled:
                    issues.append(f"#{key}: отключите стандартную автовыдачу Cardinal для этого лота.")
                break
        lines.append(f"📦 Лотов без паузы: {len(enabled)}; на паузе: {len(cfg['lots']) - len(enabled)}")
        lines += ["⚠️ " + item for item in issues[:10]]
        if len(issues) > 10:
            lines.append(f"Ещё замечаний: {len(issues) - 10}. Исправьте показанные и обновите проверку.")
        elif not issues:
            lines.append("✅ Конфликтов лотов в текущем профиле не найдено")
        self.show(ctx, "🩺 Проверка готовности\n\n" + "\n".join(lines)
                  + "\n\nЭто проверка локальных настроек по текущему профилю Cardinal. "
                  "Пароль, доступность товара и баланс здесь не проверяются. "
                  "Для проверки подключения нажмите кнопку ниже. Другие плагины выдачи нужно отключить самостоятельно.",
                  [[("🔍 Проверить подключение и баланс", "balance")], [("📦 Исправить настройки лотов", "lots:0")],
                   [("🔄 Проверить ещё раз", "diagnostics")], [("⬅️ В главное меню", "home")]])

    def summary(self, ctx):
        self.clear(ctx)
        data = self.e.ledger.summary()
        self.show(ctx, "📊 Сводка заказов\n\n"
                  f"Всего в журнале: {data['total']}\nНовых за последние 24 часа: {data['last24']}\n\n"
                  f"💬 Ждём покупателя: {data['waiting']}\n🟠 Требуют внимания: {data['attention']}\n"
                  f"⏳ Запрос выполняется: {data['processing']}\n"
                  f"💳 Оплата подтверждена поставщиком: {data['paid']}\n"
                  f"✍️ Завершены вручную: {data['manual']}\n🧪 Тестовые: {data['dry_run']}\n"
                  f"🚫 Закрыты / отменены: {data['cancelled']}\n\n"
                  "Состояния показаны за всё время. Подтверждённая оплата не означает независимую проверку доставки. "
                  "Закрытие записи не выполняет возврат денег.",
                  [[("📋 Открыть заказы", "orders:0")], [("🔄 Обновить сводку", "summary")],
                   [("⬅️ В главное меню", "home")]])

    def order(self, ctx, order_id):
        self.clear(ctx)
        row = self.e.ledger.get(order_id)
        if not row:
            raise GGPError("Заказ не найден.")
        text = (f"📋 Заказ #{row['id']}\n\n{KIND_NAMES[row['rule']['kind']]}\n"
                f"Статус: {STATE_NAMES.get(row['state'], row['state'])}\n"
                f"Получатель: {row.get('recipient') or 'ещё не указан'}\nОбъём: {row['quantity']}\n"
                f"Лот: #{row['lot_id']}")
        if row.get("error"):
            text += "\n\nЧто случилось: " + row["error"][:500]
        if row["state"] in {"review", "created"}:
            text += "\n\nПроверьте списание и доставку у GreenGamePay. Повтор оплаты заблокирован.\nID поставщика: " + row["custom_id"]
        if row.get("manual_resolution"):
            text += "\n\nПродавец закрыл запись вручную. Это не подтверждение оплаты от API."
        rows = []
        if row["state"] in {"blocked", "ready"}:
            rows.append([("▶️ Возобновить обработку", "resumeask:" + order_id)])
        if row["state"] in {"paid", "done"} and not row.get("manual_resolution"):
            rows.append([("💬 Повторить сообщение", "redeliver:" + order_id)])
        if row["state"] not in IN_FLIGHT | {"done", "cancelled"}:
            rows.append([("✍️ Закрыть запись вручную", "resolveask:" + order_id)])
        rows += [[("🔄 Обновить статус", "order:" + order_id)], [("⬅️ К заказам", "orders:0")]]
        self.show(ctx, text, rows)

    def callback(self, call):
        if (not getattr(call, "message", None) or not getattr(call, "from_user", None)
                or call.from_user.id not in self.e.c.telegram.authorized_users
                or call.message.chat.type != "private"):
            return
        ctx = self.context(call.message, call.from_user.id, edit=True)
        parts = call.data.split(":", 3)
        session = self.sessions.get(self.key(ctx), {})
        if (len(parts) < 3 or parts[1] != session.get("token") or ctx["mid"] != session.get("mid")):
            self.e.c.telegram.bot.send_message(ctx["chat"], "Это старое меню. Откройте актуальное командой /ggp.")
            return
        action, arg = parts[2], parts[3] if len(parts) == 4 else ""
        try:
            self.route(ctx, action, arg, call)
        except (GGPError, ValueError, TypeError, KeyError) as exc:
            text = str(exc) if isinstance(exc, GGPError) else "Не удалось обработать ответ. Обновите меню и попробуйте ещё раз."
            self.show(ctx, "⚠️ Не удалось выполнить действие\n\n" + text,
                      [[("⬅️ В главное меню", "home")]])

    def route(self, ctx, action, arg, call):
        cfg = self.e.config.snapshot()
        if action == "home":
            self.home(ctx)
        elif action == "diagnostics":
            self.diagnostics(ctx)
        elif action == "summary":
            self.summary(ctx)
        elif action == "findorder":
            self.begin(ctx, "findorder")
        elif action == "calculate":
            rule = cfg["lots"].get(arg)
            if not rule:
                raise GGPError("Сначала настройте этот лот.")
            self.begin(ctx, "calculate", lot_id=arg, rule=copy.deepcopy(rule))
        elif action in {"lotpause", "lotresume"}:
            if arg not in cfg["lots"]:
                raise GGPError("Привязка лота не найдена.")
            cfg["lots"][arg]["enabled"] = action == "lotresume"
            self.e.config.save(cfg)
            self.lot(ctx, arg)
        elif action in {"enable", "disable", "test", "live_yes"}:
            self.clear(ctx)
            if action == "enable" and self.e.c.old_mode_enabled:
                raise GGPError("В настройках Cardinal отключите старый режим получения сообщений (oldMsgGetMode).")
            if action == "live_yes":
                if not cfg["lots"] or not ((os.environ.get("GGP_USERNAME") or cfg["username"])
                                           and (os.environ.get("GGP_PASSWORD") or cfg["password"])):
                    raise GGPError("Сначала подключите GreenGamePay и настройте хотя бы один лот.")
                cfg["dry_run"] = False
            elif action == "test":
                cfg["dry_run"] = True
            else:
                cfg["enabled"] = action == "enable"
                if action == "enable":
                    self.e.paused.clear()
                else:
                    self.e.paused.set()
            self.e.config.save(cfg)
            self.home(ctx)
        elif action == "live":
            self.clear(ctx)
            self.show(ctx, "💳 Перейти к реальным покупкам?\n\n"
                      "После подтверждения покупателем плагин будет оплачивать товар с вашего баланса GreenGamePay.\n"
                      "Проверьте привязки лотов и лимиты.\n\n"
                      "Уже завершённые тестовые заказы останутся тестовыми.",
                      [[("💳 Да, включить реальные покупки", "live_yes")], [("🧪 Оставить тестовый режим", "home")]])
        elif action == "connect":
            self.clear(ctx)
            external = bool(os.environ.get("GGP_USERNAME") or os.environ.get("GGP_PASSWORD"))
            text = "🔑 Подключение GreenGamePay\n\nНужны логин и пароль от пополнистим.рф. GitHub-токен здесь не используется.\n"
            text += ("\nДанные заданы через переменные окружения сервера. Меняйте их на сервере и перезапускайте Cardinal."
                     if external else "\nНажмите кнопку ниже: бот по очереди запросит логин и пароль.")
            rows = [] if external else [[("✏️ Ввести логин и пароль", "auth")]]
            rows += [[("🔍 Проверить подключение и баланс", "balance")], [("⬅️ В главное меню", "home")]]
            self.show(ctx, text, rows)
        elif action == "auth":
            self.begin(ctx, "login")
        elif action == "balance":
            self.clear(ctx)
            usd = number(self.e.api.call("check_balance"), "баланс USD", allow_zero=True)
            ton = number(self.e.api.call("get_ton_balance"), "баланс TON", allow_zero=True)
            self.show(ctx, f"💰 Баланс GreenGamePay\n\n✅ Подключение работает\n\n💵 {usd} USD — Steam и подарки Steam\n"
                      f"💎 {ton} TON — Stars и Premium\n\nПополнение баланса выполняется на стороне GreenGamePay.",
                      [[("🔄 Обновить баланс", "balance")], [("⬅️ В главное меню", "home")]])
        elif action == "lots":
            self.lots(ctx, int(arg))
        elif action == "lot":
            self.lot(ctx, str(integer(arg, "ID лота")))
        elif action == "setup":
            lot_id = str(integer(arg, "ID лота"))
            if int(lot_id) not in self.e.c.profile.get_sorted_lots(1):
                raise GGPError("Лот не найден. Обновите профиль Cardinal.")
            self.begin(ctx, "kind", lot_id=lot_id,
                       rule={"enabled": cfg["lots"].get(lot_id, {}).get("enabled", True)})
        elif action in {"kind", "currency", "months", "region", "save"}:
            self.wizard_action(ctx, action, arg)
        elif action == "unask":
            self.show(ctx, "🗑 Отключить лот?\n\nНовые заказы этого лота не будут обрабатываться GreenGamePay.\n"
                      "Существующие заказы сохранят прежние настройки.",
                      [[("🗑 Да, отключить лот", "unbind:" + arg)], [("⬅️ Оставить подключённым", "lot:" + arg)]])
        elif action == "unbind":
            cfg["lots"].pop(str(integer(arg)), None)
            self.e.config.save(cfg)
            self.lot(ctx, arg)
        elif action == "orders":
            self.orders(ctx, int(arg))
        elif action == "order":
            self.order(ctx, arg)
        elif action == "resumeask":
            self.show(ctx, "▶️ Возобновить обработку?\n\nВ тестовом режиме будет выполнен тест. "
                      "В реальном режиме возможна покупка с баланса GreenGamePay.\n"
                      "Продолжение разрешено только для заказов, по которым запрос создания/оплаты ещё не отправлялся.",
                      [[("▶️ Продолжить", "resume:" + arg)], [("⬅️ Назад к заказу", "order:" + arg)]])
        elif action == "resume":
            row = self.e.ledger.get(arg)
            if not row or row["state"] not in {"blocked", "ready"} or not self.e.active():
                raise GGPError("Этот заказ нельзя возобновить, либо автопокупка выключена.")
            self.e.phase(row, "ready")
            self.e.process(row)
            self.order(ctx, arg)
        elif action == "redeliver":
            row = self.e.ledger.get(arg)
            if not row or row["state"] not in {"paid", "done"} or row.get("manual_resolution"):
                raise GGPError("Нет подтверждённой оплаты поставщика.")
            self.e.phase(row, "paid")
            self.e.deliver(row)
            self.order(ctx, arg)
        elif action == "resolveask":
            self.show(ctx, "✍️ Ручное закрытие записи\n\nИспользуйте только после проверки у поставщика.\n"
                      "Эти кнопки меняют запись в журнале. Они не покупают товар, не возвращают деньги и не закрывают заказ FunPay.",
                      [[("✅ Доставка проверена — завершить", "finish:" + arg)],
                       [("🚫 Закрыть без отметки о доставке", "cancelorder:" + arg)],
                       [("⬅️ Назад к заказу", "order:" + arg)]])
        elif action in {"finish", "cancelorder"}:
            row = self.e.ledger.get(arg)
            if not row or row["state"] in IN_FLIGHT | {"done", "cancelled"}:
                raise GGPError("Запись уже закрыта или запрос ещё выполняется.")
            self.e.phase(row, "done" if action == "finish" else "cancelled",
                         manual_resolution=True, resolved_by=ctx["user"])
            self.order(ctx, arg)
        elif action == "catalog":
            self.clear(ctx)
            catalog = self.e.api.call("get_all_services")
            items = [item for cat in catalog.get("categories", []) for item in cat.get("services", [])]
            page = max(0, min(int(arg), max(0, (len(items) - 1) // 6)))
            lines = [f"• {str(item.get('service_name', 'Товар'))[:180]}\n"
                     f"  {item.get('price')} {item.get('currency')} · остаток {item.get('in_stock')}"
                     for item in items[page * 6:page * 6 + 6]]
            rows = []
            self.pager(rows, "catalog", page, len(items), 6)
            rows.append([("⬅️ В главное меню", "home")])
            self.show(ctx, "📚 Каталог поставщика\n\n" + ("\n\n".join(lines) or "Каталог пуст.")
                      + "\n\nКаталог справочный. Плагин автоматизирует Steam, Stars, Premium и подарки Steam; подарочные коды не выдаёт.", rows)
        elif action == "help":
            self.clear(ctx)
            self.show(ctx, "❓ Как это работает\n\n"
                      "1️⃣ Подключение: введите данные GreenGamePay и проверьте баланс.\n\n"
                      "2️⃣ Мои лоты: выберите товар FunPay, тип услуги, объём и лимит. Отключите стандартную автовыдачу для этого лота.\n\n"
                      "3️⃣ Тестовый режим: покупатель проходит сценарий, но GreenGamePay не получает запрос покупки.\n\n"
                      "4️⃣ Реальные покупки: выключите тестовый режим и включите автопокупку. Покупатель указывает и подтверждает получателя.\n\n"
                      "Объём за заказ = объём за 1 единицу × число купленных единиц. Цена продажи FunPay не определяет объём.\n\n"
                      "Если статус «Нужна ручная проверка», сначала проверьте списание у поставщика. "
                      "Повторная оплата автоматически не выполняется. Журнал заказов не очищается, чтобы избежать повторных покупок.\n\n"
                      "Для Stars/Premium лимит относится к котировке, итог может включать комиссию. "
                      "Если цена Premium пришла в неизвестном формате, покупка остановится.",
                      [[("🚀 Открыть подключение", "connect")], [("📦 Настроить лоты", "lots:0")], [("⬅️ В главное меню", "home")]])


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
                _engine.panel.clear(_engine.panel.context(message))
                _engine.submit(telegram_command, _engine, message)

        cardinal.telegram.msg_handler(command, commands=commands)
        from tg_bot import CBT

        def settings(call):
            if (call.from_user.id not in cardinal.telegram.authorized_users
                    or not call.message or call.message.chat.type != "private"):
                return
            cardinal.telegram.bot.answer_callback_query(call.id)
            _engine.submit(_engine.panel.home, _engine.panel.context(call.message, call.from_user.id, edit=True))

        def panel_callback(call):
            if (not call.message or not call.from_user or call.from_user.id not in cardinal.telegram.authorized_users
                    or call.message.chat.type != "private"):
                return
            cardinal.telegram.bot.answer_callback_query(call.id)
            # Apply emergency pause before waiting for the serialized worker, but only for a current button.
            parts = call.data.split(":", 3)
            session = _engine.panel.sessions.get((call.message.chat.id, call.from_user.id), {})
            if (len(parts) == 3 and parts[2] == "disable" and parts[1] == session.get("token")
                    and call.message.id == session.get("mid")):
                _engine.paused.set()
            _engine.submit(_engine.panel.callback, call)

        cardinal.telegram.cbq_handler(settings, lambda call: bool(call.data) and call.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}:"))
        cardinal.telegram.cbq_handler(panel_callback, lambda call: bool(call.data) and call.data.startswith("ggp:"))
        cardinal.telegram.msg_handler(lambda message: _engine.submit(_engine.panel.input, message),
                                      func=_engine.panel.has_input, content_types=["text"])
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
