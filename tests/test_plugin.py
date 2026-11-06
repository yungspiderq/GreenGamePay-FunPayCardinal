import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
import requests

import greengamepay as g


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.cost = 1
        self.overrides = {}

    def call(self, endpoint, payload=None, **kwargs):
        self.calls.append((endpoint, copy.deepcopy(payload)))
        if endpoint == self.fail:
            raise g.GGPError("Таймаут")
        if endpoint in self.overrides:
            return self.overrides[endpoint]
        if endpoint == "check":
            return {"result": True}
        if endpoint in {"rates", "get_amount"}:
            return {"usd_price": 2}
        if endpoint == "create_order":
            return {"custom_id": payload["custom_id"], "total": self.cost}
        if endpoint == "steam_gift_create_order":
            return {"custom_id": "777d8536-32ed-4fd4-a546-379ff97ec902", "total": self.cost}
        if endpoint in {"pay_order", "steam_gift_pay_order"}:
            return {"custom_id": payload["custom_id"], "msg": "Successfully paid the order"}
        if endpoint in {"get_stars_price", "get_premium_price"}:
            return {"ton": self.cost}
        if endpoint == "steam_gift_calculate":
            return {"price": self.cost}
        if endpoint == "get_ton_balance":
            return 20
        if endpoint == "checkStarsRecipient":
            return {"msg": "success", "min_stars": 50}
        if endpoint in {"buyStars", "buyPremium"}:
            return {"msg": "success", "username": payload["username"], "tx_id": "tx",
                    "stars_amount": payload.get("quantity"), "period": payload.get("months")}
        raise AssertionError(endpoint)


@pytest.fixture
def env(tmp_path):
    category = NS(id=10)
    lot = NS(id=123, description="Steam unique", subcategory=category, server=None, side=None)
    order = NS(id="AB123", status=NS(name="PAID"), seller_id=1, buyer_id=2, chat_id=3,
               buyer_username="buyer", short_description=lot.description, amount=1,
               subcategory=category, server=None, side=None)
    c = NS(plugins={g.UUID: NS(enabled=True)}, account=NS(id=1, get_order=Mock(return_value=order)),
           telegram=NS(send_notification=Mock(), bot=NS(send_message=Mock(), delete_message=Mock()),
                       authorized_users={7: {}}), old_mode_enabled=False, bl_delivery_enabled=True,
           blacklist=[], send_message=Mock(return_value=True),
           profile=NS(get_sorted_lots=Mock(return_value={123: lot})))
    c.profile.id = c.account.id
    c.profile.update_lot = lambda item: c.profile.get_sorted_lots.return_value.update({item.id: item})
    c.account.get_user = Mock(return_value=c.profile)
    api = FakeAPI()
    engine = g.Engine(c, tmp_path, api=api)
    cfg = engine.config.snapshot()
    cfg.update(enabled=True, dry_run=False)
    cfg["lots"]["123"] = {"kind": "steam", "quantity": "1", "max_quantity": "1000", "max_cost": "10"}
    engine.config.save(cfg)
    event = NS(order=order, lot_id=123, lot_shortcut=lot, config_section_obj=None, delivered=False)
    yield engine, event, c, api
    engine.close()


def prepare(env, kind="steam", quantity="1"):
    engine, event, c, api = env
    cfg = engine.config.snapshot()
    cfg["lots"]["123"].update(kind=kind, quantity=quantity)
    if kind == "gift":
        cfg["lots"]["123"].update(sub_id=100, region="KZ")
    engine.config.save(cfg)
    engine.new_order(event)
    value = {"steam": "test_login", "stars": "@testuser", "premium": "@testuser",
             "gift": "https://s.team/p/abcd/efgh"}[kind]
    engine.message(NS(text="!ggp AB123 " + value, author_id=2, chat_id=3))
    row = engine.ledger.get("AB123")
    assert row["state"] == "confirm"
    return NS(text="!ggp AB123 да " + row["nonce"], author_id=2, chat_id=3)


@pytest.mark.parametrize("kind,quantity,payment", [
    ("steam", "1", "pay_order"), ("stars", "50", "buyStars"),
    ("premium", "3", "buyPremium"), ("gift", "1", "steam_gift_pay_order")])
def test_success_and_duplicate_events(env, kind, quantity, payment):
    msg = prepare(env, kind, quantity)
    engine, event, c, api = env
    engine.message(msg)
    engine.message(msg)
    engine.new_order(event)
    assert engine.ledger.get("AB123")["state"] == "done"
    assert sum(endpoint == payment for endpoint, _ in api.calls) == 1


def test_dry_run_never_contacts_provider(env):
    msg = prepare(env)
    engine, _, _, api = env
    cfg = engine.config.snapshot()
    cfg["dry_run"] = True
    engine.config.save(cfg)
    engine.message(msg)
    assert not api.calls
    assert engine.ledger.get("AB123")["state"] == "dry_run"


@pytest.mark.parametrize("endpoint", ["create_order", "pay_order"])
def test_ambiguous_request_never_retried(env, endpoint):
    msg = prepare(env)
    engine, event, _, api = env
    api.fail = endpoint
    engine.message(msg)
    assert engine.ledger.get("AB123")["state"] == "review"
    engine.message(msg)
    engine.new_order(event)
    admin(engine, "/ggp_resume AB123")
    assert sum(name == endpoint for name, _ in api.calls) == 1


def test_preflight_failure_can_resume(env):
    msg = prepare(env)
    engine, _, _, api = env
    api.fail = "check"
    engine.message(msg)
    assert engine.ledger.get("AB123")["state"] == "blocked"
    api.fail = None
    admin(engine, "/ggp_resume AB123")
    assert engine.ledger.get("AB123")["state"] == "done"


def test_price_limit_stops_before_payment(env):
    msg = prepare(env)
    engine, _, _, api = env
    api.cost = 11
    engine.message(msg)
    assert "pay_order" not in [name for name, _ in api.calls]
    assert engine.ledger.get("AB123")["state"] == "review"


@pytest.mark.parametrize("author,chat", [(5, 3), (2, 8), (0, 3), (1, 3)])
def test_other_user_or_chat_cannot_confirm(env, author, chat):
    msg = prepare(env)
    msg.author_id, msg.chat_id = author, chat
    env[0].message(msg)
    assert not env[3].calls


def test_confirmation_nonce_and_expiry(env):
    msg = prepare(env)
    engine = env[0]
    wrong = copy.copy(msg)
    wrong.text = "!ggp AB123 да 0000000"
    engine.message(wrong)
    assert not env[3].calls
    row = engine.ledger.get("AB123")
    row["confirm_until"] = time.time() - 1
    engine.ledger.put(row)
    engine.message(msg)
    assert not env[3].calls
    assert engine.ledger.get("AB123")["state"] == "waiting"


def test_refund_before_confirmation(env):
    msg = prepare(env)
    env[1].order.status.name = "REFUNDED"
    env[0].message(msg)
    assert not env[3].calls


def test_refund_between_create_and_pay(env):
    msg = prepare(env)
    original = env[3].call
    def call(endpoint, payload=None, **kwargs):
        result = original(endpoint, payload, **kwargs)
        if endpoint == "create_order":
            env[1].order.status.name = "REFUNDED"
        return result
    env[3].call = call
    env[0].message(msg)
    assert "pay_order" not in [name for name, _ in env[3].calls]
    assert env[0].ledger.get("AB123")["state"] == "review"


def test_disable_between_create_and_pay(env):
    msg = prepare(env)
    original = env[3].call
    def call(endpoint, payload=None, **kwargs):
        result = original(endpoint, payload, **kwargs)
        if endpoint == "create_order":
            env[2].plugins[g.UUID].enabled = False
        return result
    env[3].call = call
    env[0].message(msg)
    assert "pay_order" not in [name for name, _ in env[3].calls]


def test_failed_chat_delivery_can_resend_without_buying(env):
    msg = prepare(env)
    engine, _, c, api = env
    c.send_message.return_value = False
    engine.message(msg)
    assert engine.ledger.get("AB123")["state"] == "paid"
    count = len(api.calls)
    c.send_message.return_value = True
    admin(engine, "/ggp_redeliver AB123")
    assert len(api.calls) == count
    assert engine.ledger.get("AB123")["state"] == "done"


@pytest.mark.parametrize("state", ["creating", "paying"])
def test_restart_quarantines_in_flight(env, state):
    prepare(env)
    engine = env[0]
    row = engine.ledger.get("AB123")
    engine.phase(row, state)
    g.Ledger(engine.config.path.parent).recover()
    assert engine.ledger.get("AB123")["state"] == "review"


def test_second_instance_is_rejected(env):
    with pytest.raises(g.GGPError):
        g.InstanceLock(env[0].config.path.parent)


def test_ledger_atomic_duplicate_insert(tmp_path):
    ledger = g.Ledger(tmp_path)
    row = {"id": "X", "state": "waiting"}
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: ledger.add(row), range(20)))
    assert sum(results) == 1


@pytest.mark.parametrize("change", ["title", "buyer", "seller", "category", "duplicate", "native", "old_mode", "blacklist"])
def test_wrong_or_ambiguous_order_is_not_purchased(env, change):
    engine, event, c, api = env
    if change == "title":
        event.order.short_description = "forged title"
    elif change == "buyer":
        event.order = copy.copy(event.order)
        event.order.buyer_id = 99
    elif change == "seller":
        event.order.seller_id = 99
    elif change == "category":
        event.order.subcategory = NS(id=99)
    elif change == "duplicate":
        twin = copy.copy(event.lot_shortcut)
        twin.id = 124
        c.profile.get_sorted_lots.return_value[124] = twin
    elif change == "native":
        event.delivered = True
    elif change == "old_mode":
        c.old_mode_enabled = True
    elif change == "blacklist":
        c.blacklist.append("buyer")
    engine.new_order(event)
    assert engine.ledger.get("AB123") is None
    assert not api.calls


@pytest.mark.parametrize("currency,endpoint", [("RUB", "get_amount"), ("UAH", "rates"), ("KZT", "rates")])
def test_currency_conversion_and_units(env, currency, endpoint):
    engine, event, _, api = env
    event.order.amount = 2
    cfg = engine.config.snapshot()
    cfg["lots"]["123"].update(currency=currency, quantity="100")
    engine.config.save(cfg)
    engine.new_order(event)
    engine.message(NS(text="!ggp AB123 test_login", author_id=2, chat_id=3))
    nonce = engine.ledger.get("AB123")["nonce"]
    engine.message(NS(text="!ggp AB123 да " + nonce, author_id=2, chat_id=3))
    assert dict(api.calls)[endpoint]["amount"] == 200
    assert dict(api.calls)["create_order"]["quantity"] == 2


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1", "0", "bad", None])
def test_invalid_numbers(bad):
    with pytest.raises(g.GGPError):
        g.number(bad)


@pytest.mark.parametrize("kind,value", [("steam", "https://steamcommunity.com/id/x"),
    ("stars", "@bad user"), ("gift", "https://s.team.evil.test/p/x"),
    ("gift", "https://evil@s.team/p/x"), ("gift", "http://s.team/p/x")])
def test_invalid_recipients(kind, value):
    with pytest.raises(g.GGPError):
        g.recipient(kind, value)


def admin(engine, text, user=7, chat_type="private"):
    message = NS(text=text, from_user=NS(id=user), chat=NS(id=user, type=chat_type), id=1)
    g.telegram_command(engine, message)


@pytest.mark.parametrize("user,chat_type", [(8, "private"), (7, "group")])
def test_telegram_access_control(env, user, chat_type):
    engine = env[0]
    before = engine.config.snapshot()
    admin(engine, "/ggp_off", user, chat_type)
    assert engine.config.snapshot() == before


def test_config_corruption_is_not_silently_reset(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("BROKEN", encoding="utf8")
    with pytest.raises(ValueError):
        g.Config(tmp_path)
    assert path.read_text() == "BROKEN"


def test_http_token_cache_and_no_retry(tmp_path):
    cfg = g.Config(tmp_path)
    data = cfg.snapshot()
    data.update(username="user", password="secret")
    cfg.save(data)
    session = Mock()
    token = Mock(status_code=200)
    token.json.return_value = {"access_token": "hidden-token", "expired": time.time() + 3600}
    ok = Mock(status_code=200)
    ok.json.return_value = 50
    denied = Mock(status_code=403)
    denied.json.return_value = {"detail": "hidden-token secret"}
    session.request.side_effect = [token, ok, denied]
    api = g.API(cfg, session)
    assert api.call("check_balance") == 50
    with pytest.raises(g.GGPError) as error:
        api.call("pay_order", {"custom_id": "id"})
    assert "secret" not in str(error.value) and "hidden-token" not in str(error.value)
    assert session.request.call_count == 3
    assert api.token is None
    for call in session.request.call_args_list:
        assert call.kwargs["allow_redirects"] is False
        assert call.kwargs["timeout"] == (10, 60)


@pytest.mark.parametrize("failure", [requests.Timeout("secret"), "bad_json", "body_error", "redirect"])
def test_http_protocol_errors_are_safe(tmp_path, failure):
    api = g.API(g.Config(tmp_path), Mock())
    response = Mock(status_code=200)
    if isinstance(failure, Exception):
        api.session.request.side_effect = failure
    else:
        api.session.request.return_value = response
        if failure == "bad_json":
            response.json.side_effect = ValueError("secret")
        elif failure == "body_error":
            response.json.return_value = {"detail": "secret"}
        else:
            response.status_code = 302
    with pytest.raises(g.GGPError) as error:
        api._request("test")
    assert "secret" not in str(error.value)
    assert api.session.request.call_count == 1


def test_unknown_premium_quote_stops_safely(env):
    msg = prepare(env, "premium", "3")
    env[3].overrides["get_premium_price"] = {"unknown": 1}
    env[0].message(msg)
    assert "buyPremium" not in [name for name, _ in env[3].calls]
    assert env[0].ledger.get("AB123")["state"] == "blocked"


def test_unconfirmed_success_body_is_reviewed(env):
    msg = prepare(env)
    env[3].overrides["pay_order"] = {"msg": "queued"}
    env[0].message(msg)
    assert env[0].ledger.get("AB123")["state"] == "review"


def test_worker_serializes_repeated_confirmation(env):
    msg = prepare(env)
    engine = env[0]
    done = threading.Event()
    for _ in range(10):
        engine.submit(engine.message, msg)
    engine.submit(done.set)
    assert done.wait(10)
    assert sum(name == "pay_order" for name, _ in env[3].calls) == 1


def test_rule_snapshot_survives_config_change(env):
    msg = prepare(env)
    cfg = env[0].config.snapshot()
    cfg["lots"]["123"]["quantity"] = "100"
    env[0].config.save(cfg)
    env[0].message(msg)
    assert dict(env[3].calls)["create_order"]["quantity"] == 1


def test_metadata_and_hooks():
    import uuid
    assert uuid.UUID(g.UUID).version == 4
    for field in ["NAME", "VERSION", "DESCRIPTION", "CREDITS", "SETTINGS_PAGE", "UUID", "BIND_TO_DELETE"]:
        assert hasattr(g, field)
    assert g.BIND_TO_NEW_ORDER == [g.new_order]
    assert callable(g.BIND_TO_DELETE)


def test_json_contains_no_auth_in_order(env):
    prepare(env)
    row = json.dumps(env[0].ledger.get("AB123"))
    assert "password" not in row and "access_token" not in row


class PanelDriver:
    def __init__(self, engine):
        self.engine = engine
        self.panel = engine.panel
        self.bot = engine.c.telegram.bot
        self.bot.send_message.return_value = NS(message_id=100)
        self.bot.edit_message_text = Mock()
        self.bot.answer_callback_query = Mock()
        self.message = NS(id=100, chat=NS(id=7, type="private"), from_user=NS(id=7), text="/ggp")
        self.states = {}
        tg = engine.c.telegram
        tg.get_state = lambda chat, user: self.states.get((chat, user))
        tg.set_state = lambda chat, mid, user, state: self.states.update({(chat, user): {"state": state}})
        tg.clear_state = lambda chat, user: self.states.pop((chat, user), None)
        self.panel.open(self.message)

    @property
    def view(self):
        # All screens after the first edit the same message.
        return self.bot.edit_message_text.call_args or self.bot.send_message.call_args

    @property
    def text(self):
        return self.view.args[0] if self.bot.edit_message_text.call_args else self.view.args[1]

    @property
    def buttons(self):
        return [button for row in self.view.kwargs["reply_markup"].keyboard for button in row]

    def data(self, action):
        return next(button.callback_data for button in self.buttons if button.callback_data.split(":", 2)[-1] == action)

    def click(self, action=None, data=None, user=7, chat=7, chat_type="private"):
        self.panel.callback(NS(id="cb", data=data or self.data(action), from_user=NS(id=user),
                               message=NS(id=100, chat=NS(id=chat, type=chat_type))))

    def type(self, text):
        self.panel.input(NS(id=200, chat=self.message.chat, from_user=self.message.from_user, text=text))


@pytest.fixture
def ui(env, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "tg_bot", NS(CBT=NS(EDIT_PLUGIN="45")))
    return PanelDriver(env[0])


def test_panel_home_has_status_and_native_plugin_back(ui):
    assert "Автопокупка:" in ui.text
    assert "Подключение:" in ui.text
    assert any(button.text == "⬅️ Назад к плагину" and button.callback_data == f"45:{g.UUID}:0" for button in ui.buttons)
    assert all(len(button.callback_data.encode()) <= 64 for button in ui.buttons)


@pytest.mark.parametrize("kind,inputs", [
    ("steam", [("currency:RUB", None), (None, "100"), (None, "500"), (None, "10")]),
    ("stars", [(None, "50"), (None, "500"), (None, "5")]),
    ("premium", [("months:6", None), (None, "20")]),
    ("gift", [(None, "505826"), ("region:KZ", None), (None, "40")])])
def test_button_wizard_saves_all_service_types(ui, kind, inputs):
    ui.click("lots:0")
    ui.click("lot:123")
    ui.click("setup:123")
    ui.click("kind:" + kind)
    for button, text in inputs:
        ui.click(button) if button else ui.type(text)
    assert "Проверьте настройки" in ui.text
    assert ui.engine.config.snapshot()["lots"]["123"]["kind"] == "steam"  # no premature save
    ui.click("save")
    assert ui.engine.config.snapshot()["lots"]["123"]["kind"] == kind
    assert not ui.panel.pending
    assert not ui.states


def test_wizard_validation_and_decimal_comma(ui):
    ui.click("lots:0")
    ui.click("lot:123")
    ui.click("setup:123")
    ui.click("kind:stars")
    ui.type("10")
    assert "Минимум 50" in ui.text
    ui.type("50.5")
    assert "целым" in ui.text
    ui.type("50")
    ui.type("49")
    assert "не может быть меньше" in ui.text
    ui.type("100")
    ui.type("1,5")
    ui.click("save")
    assert ui.engine.config.snapshot()["lots"]["123"]["max_cost"] == "1.5"


def test_wizard_cancel_keeps_existing_rule(ui):
    before = ui.engine.config.snapshot()
    ui.click("lots:0")
    ui.click("lot:123")
    ui.click("setup:123")
    ui.click("kind:premium")
    ui.click("home")
    ui.type("12")
    assert ui.engine.config.snapshot() == before
    assert not ui.panel.pending and not ui.states


def test_auth_wizard_deletes_password_without_echo(ui):
    ui.click("connect")
    ui.click("auth")
    ui.type("ggp-user")
    ui.type("totally-secret-pass")
    cfg = ui.engine.config.snapshot()
    assert cfg["username"] == "ggp-user" and cfg["password"] == "totally-secret-pass"
    ui.bot.delete_message.assert_called_with(7, 200)
    assert "totally-secret-pass" not in str(ui.bot.send_message.call_args_list)
    assert "totally-secret-pass" not in str(ui.bot.edit_message_text.call_args_list)
    assert "данные указаны" in ui.text


def test_expired_password_step_still_deletes_secret(ui):
    ui.click("connect")
    ui.click("auth")
    ui.type("login")
    ui.panel.pending[(7, 7)]["expires"] = 0
    ui.type("secret")
    ui.bot.delete_message.assert_called_with(7, 200)
    assert not ui.engine.config.snapshot()["password"]
    assert "Время ввода истекло" in ui.text


def test_stale_callback_cannot_change_settings(ui):
    stale = ui.data("disable")
    ui.click("home")
    before = ui.engine.config.snapshot()
    ui.click(data=stale)
    assert ui.engine.config.snapshot() == before
    assert not ui.engine.paused.is_set()


@pytest.mark.parametrize("user,chat,chat_type", [(8, 7, "private"), (7, 7, "group"), (7, 10, "private")])
def test_panel_rejects_unauthorized_or_wrong_context(ui, user, chat, chat_type):
    before = ui.engine.config.snapshot()
    ui.click("disable", user=user, chat=chat, chat_type=chat_type)
    assert ui.engine.config.snapshot() == before


def test_live_mode_requires_explicit_confirmation(ui):
    cfg = ui.engine.config.snapshot()
    cfg.update(dry_run=True, username="u", password="p")
    ui.engine.config.save(cfg)
    ui.click("home")
    ui.click("live")
    assert ui.engine.config.snapshot()["dry_run"]
    ui.click("live_yes")
    assert ui.engine.config.snapshot()["dry_run"] is False


def test_live_mode_without_credentials_stays_test(ui):
    cfg = ui.engine.config.snapshot()
    cfg["dry_run"] = True
    ui.engine.config.save(cfg)
    ui.click("home")
    ui.click("live")
    ui.click("live_yes")
    assert ui.engine.config.snapshot()["dry_run"] is True
    assert "Сначала подключите" in ui.text


def test_disable_and_enable_are_idempotent(ui):
    ui.click("disable")
    assert not ui.engine.active()
    ui.click("enable")
    assert ui.engine.active()


def test_unbind_requires_confirmation(ui):
    ui.click("lots:0")
    ui.click("lot:123")
    ui.click("unask:123")
    assert "123" in ui.engine.config.snapshot()["lots"]
    ui.click("unbind:123")
    assert "123" not in ui.engine.config.snapshot()["lots"]


def test_orders_show_russian_status_and_review_has_no_payment_button(ui, env):
    msg = prepare(env)
    env[3].fail = "pay_order"
    env[0].message(msg)
    ui.click("orders:0")
    ui.click("order:AB123")
    assert "Нужна ручная проверка" in ui.text
    assert not any("resume" in button.callback_data for button in ui.buttons)


def test_manual_resolution_does_not_claim_provider_payment(ui, env):
    prepare(env)
    ui.click("orders:0")
    ui.click("order:AB123")
    ui.click("resolveask:AB123")
    ui.click("finish:AB123")
    assert "Продавец закрыл запись вручную" in ui.text
    assert not any("redeliver" in button.callback_data for button in ui.buttons)
    assert not env[3].calls
    admin(env[0], "/ggp_redeliver AB123")
    assert not env[3].calls


def test_lot_pagination_and_html_escaping(ui, env):
    lots = env[2].profile.get_sorted_lots.return_value
    for key in range(124, 135):
        lot = copy.copy(lots[123])
        lot.id, lot.description = key, "<b>title</b>"
        lots[key] = lot
    ui.click("lots:0")
    assert sum("lot:" in button.callback_data for button in ui.buttons) == 6
    ui.click("lots:1")
    ui.click("lot:129")
    assert "&lt;b&gt;title&lt;/b&gt;" in ui.text


def test_other_cardinal_wizard_takes_over_text_input(ui):
    ui.click("connect")
    ui.click("auth")
    ui.states[(7, 7)] = {"state": "other_plugin"}
    ui.type("not-a-ggp-login")
    assert not ui.panel.pending
    assert not ui.engine.config.snapshot()["username"]


def test_balance_button_checks_both_currencies(ui, env):
    env[3].overrides["check_balance"] = 12.5
    ui.click("balance")
    assert "12.5 USD" in ui.text and "20 TON" in ui.text
    assert "Подключение работает" in ui.text


def test_panel_environment_credentials_are_not_overwritten_by_wizard(ui, monkeypatch):
    monkeypatch.setenv("GGP_USERNAME", "external")
    ui.click("connect")
    assert "переменные окружения" in ui.text
    assert not any(button.callback_data.endswith(":auth") for button in ui.buttons)


def test_steam_settings_explain_100_rubles(ui):
    ui.click("lots:0")
    ui.click("lot:123")
    ui.click("setup:123")
    ui.click("kind:steam")
    ui.click("currency:RUB")
    assert "100 RUB" in ui.text and "200 RUB" in ui.text
    assert "Цену продажи" in ui.text


def test_calculator_matches_real_order_quantity_without_api(ui, env):
    cfg = ui.engine.config.snapshot()
    cfg["lots"]["123"].update(quantity="100", currency="RUB", max_quantity="500")
    ui.engine.config.save(cfg)
    ui.click("lots:0")
    ui.click("lot:123")
    assert "Сумма пополнения за 1 штуку лота: 100 RUB" in ui.text
    ui.click("calculate:123")
    ui.type("2")
    assert "Итого получит: 200 RUB" in ui.text
    assert not env[3].calls
    assert not ui.engine.ledger.recent()
    assert ui.engine.config.snapshot() == cfg


@pytest.mark.parametrize("kind,quantity,units", [("steam", "100", "6"), ("stars", "50", "11"),
    ("premium", "3", "2"), ("gift", "1", "2")])
def test_calculator_rejects_limits_and_subscription_multiples(ui, kind, quantity, units):
    cfg = ui.engine.config.snapshot()
    rule = dict(kind=kind, quantity=quantity, max_quantity="500", max_cost="10")
    if kind == "gift":
        rule.update(sub_id=123, region="KZ")
    cfg["lots"]["123"] = rule
    ui.engine.config.save(cfg)
    ui.click("lots:0")
    ui.click("lot:123")
    ui.click("calculate:123")
    ui.type(units)
    assert "⚠️" in ui.text
    assert (7, 7) in ui.panel.pending  # corrected input is still accepted
    ui.type("1")
    assert "Итого получит:" in ui.text


def test_pause_skips_new_orders_without_deleting_configuration(ui, env):
    ui.click("lots:0")
    ui.click("lot:123")
    ui.click("lotpause:123")
    assert ui.engine.config.snapshot()["lots"]["123"]["enabled"] is False
    env[0].new_order(env[1])
    assert not env[0].ledger.get("AB123")
    assert not env[3].calls
    ui.click("lotresume:123")
    env[0].new_order(env[1])
    assert env[0].ledger.get("AB123")["state"] == "waiting"


def test_pausing_lot_keeps_accepted_order_running(ui, env):
    msg = prepare(env)
    ui.click("lots:0")
    ui.click("lot:123")
    ui.click("lotpause:123")
    env[0].message(msg)
    assert env[0].ledger.get("AB123")["state"] == "done"


def test_editing_paused_lot_does_not_resume_it(ui):
    ui.click("lots:0")
    ui.click("lot:123")
    ui.click("lotpause:123")
    ui.click("setup:123")
    ui.click("kind:stars")
    for value in ["50", "100", "5"]:
        ui.type(value)
    ui.click("save")
    assert ui.engine.config.snapshot()["lots"]["123"]["enabled"] is False


def test_invalid_lot_enabled_flag_is_rejected():
    with pytest.raises(g.GGPError):
        g.validate_rule(dict(kind="steam", quantity="1", max_quantity="5", max_cost="10", enabled="false"))


def test_find_order_accepts_lowercase_and_hash(ui, env):
    prepare(env)
    ui.click("orders:0")
    ui.click("findorder")
    ui.type("#ab123")
    assert "Заказ #AB123" in ui.text
    assert not ui.panel.pending


def test_find_missing_order_can_be_corrected(ui, env):
    prepare(env)
    ui.click("orders:0")
    ui.click("findorder")
    ui.type("MISSING")
    assert "не найден" in ui.text
    ui.type("AB123")
    assert "Заказ #AB123" in ui.text


def test_summary_counts_all_records_and_separates_manual_and_tests(tmp_path):
    ledger = g.Ledger(tmp_path)
    now = time.time()
    rows = [("done", False), ("done", True), ("paid", False), ("dry_run", False),
            ("cancelled", False), ("confirm", False), ("review", False), ("paying", False)]
    for i, (state, manual) in enumerate(rows):
        ledger.add(dict(id=str(i), state=state, manual_resolution=manual, created=now - i * 20000))
    # Beyond the 100-entry panel window: the summary must still count all history.
    for i in range(105):
        ledger.add(dict(id="old" + str(i), state="done", created=now - 200000))
    data = ledger.summary(now)
    assert data == dict(total=113, last24=5, waiting=1, attention=1, paid=107,
                        manual=1, dry_run=1, cancelled=1, processing=1)


def test_summary_screen(ui, env):
    prepare(env)
    ui.click("summary")
    assert "Всего в журнале: 1" in ui.text
    assert "Ждём покупателя: 1" in ui.text
    assert not env[3].calls


def test_readiness_finds_conflicts_without_provider_calls(ui, env):
    import configparser
    c = env[2]
    c.old_mode_enabled = True
    c.AD_CFG = configparser.ConfigParser()
    c.AD_CFG["Steam unique"] = {"disable": "0"}
    lot = env[1].lot_shortcut
    lot.auto = True
    twin = copy.copy(lot)
    twin.id = 124
    c.profile.get_sorted_lots.return_value[124] = twin
    ui.click("diagnostics")
    assert "Отключите старый режим" in ui.text
    assert "совпадающие названия" in ui.text
    assert "автовыдачу самого FunPay" in ui.text
    assert "стандартную автовыдачу Cardinal" in ui.text
    assert not env[3].calls


def test_readiness_uses_most_specific_cardinal_rule(ui, env):
    import configparser
    c = env[2]
    c.AD_CFG = configparser.ConfigParser()
    c.AD_CFG["Steam"] = {"disable": "0"}
    c.AD_CFG["Steam unique"] = {"disable": "1"}
    ui.click("diagnostics")
    assert "Конфликтов лотов" in ui.text
    assert "отключите стандартную автовыдачу" not in ui.text


def test_readiness_reports_missing_lot(ui, env):
    env[2].profile.get_sorted_lots.return_value.clear()
    ui.click("diagnostics")
    assert "#123: лот не найден" in ui.text


@pytest.mark.parametrize("auto,secrets", [(True, []), (False, ["already-delivered-code"])])
def test_funpay_native_delivery_cannot_trigger_second_purchase(env, auto, secrets):
    engine, event, _, api = env
    event.lot_shortcut.auto = auto
    event.order.order_secrets = secrets
    engine.new_order(event)
    assert engine.ledger.get("AB123") is None
    assert not api.calls


@pytest.fixture
def guard_env(env):
    engine, _, c, api = env
    cfg = engine.config.snapshot()
    cfg.update(username="user", password="password", dry_run=False)
    cfg["balance_guard"] = {"enabled": True, "USD": "10", "TON": "2"}
    engine.config.save(cfg)
    offers = {123: NS(lot_id=123, active=True, price=12, title="Original title", description="keep me")}
    c.account.get_lot_fields = Mock(side_effect=lambda lot_id: copy.deepcopy(offers[lot_id]))
    def save(fields, *args, **kwargs):
        offers[fields.lot_id] = copy.deepcopy(fields)
    original = Mock(side_effect=save)
    c.account.save_lot = original
    api.overrides.update(check_balance=5, get_ton_balance=1)
    return engine, c, api, offers, original


def test_guard_really_disables_offer_and_preserves_other_fields(guard_env):
    engine, _, _, offers, save = guard_env
    engine.balance_guard.check()
    assert not offers[123].active
    assert offers[123].price == 12 and offers[123].title == "Original title"
    assert offers[123].description == "keep me"
    assert save.call_count == 1
    assert engine.ledger.holds()["123"]["disabled"] is True
    engine.balance_guard.check()
    assert save.call_count == 1


@pytest.mark.parametrize("balance,threshold,disabled", [(10, "10", True), (10.01, "10", False),
    (0, "0", True), (0.01, "0", False)])
def test_guard_boundary_and_zero(guard_env, balance, threshold, disabled):
    engine, _, api, offers, _ = guard_env
    cfg = engine.config.snapshot()
    cfg["balance_guard"]["USD"] = threshold
    engine.config.save(cfg)
    api.overrides["check_balance"] = balance
    engine.balance_guard.check()
    assert offers[123].active is not disabled


@pytest.mark.parametrize("response", [None, True, {}, {"balance": 0}, "NaN", "Infinity", -1, "unavailable"])
def test_guard_invalid_balance_never_means_zero(guard_env, response):
    engine, _, api, offers, save = guard_env
    api.overrides["check_balance"] = response
    engine.balance_guard.check()
    assert offers[123].active
    assert not save.called and not engine.ledger.holds()
    assert engine.balance_guard.status["USD"]["error"] is True


def test_guard_network_error_does_not_remove_existing_hold_or_spam(guard_env):
    engine, c, api, offers, save = guard_env
    engine.balance_guard.check()
    before = c.telegram.send_notification.call_count
    api.fail = "check_balance"
    engine.balance_guard.check()
    engine.balance_guard.check()
    assert c.telegram.send_notification.call_count == before + 1
    assert "123" in engine.ledger.holds() and save.call_count == 1
    assert not offers[123].active


def test_guard_uses_correct_currency_and_ignores_unmapped_lots(guard_env):
    engine, _, api, offers, save = guard_env
    cfg = engine.config.snapshot()
    cfg["lots"]["124"] = dict(kind="stars", quantity="50", max_quantity="100", max_cost="5")
    cfg["lots"]["125"] = dict(kind="premium", quantity="3", max_quantity="3", max_cost="10")
    engine.config.save(cfg)
    for lot_id in [124, 125, 999]:
        offers[lot_id] = NS(lot_id=lot_id, active=True)
    api.overrides["check_balance"] = 100
    engine.balance_guard.check()
    assert offers[123].active and offers[999].active
    assert not offers[124].active and not offers[125].active
    assert save.call_count == 2


def test_guard_disabled_and_test_mode_do_not_mutate_funpay(guard_env):
    engine, _, api, offers, save = guard_env
    cfg = engine.config.snapshot()
    cfg["balance_guard"]["enabled"] = False
    engine.config.save(cfg)
    engine.balance_guard.check()
    assert not api.calls
    cfg["balance_guard"]["enabled"] = True
    cfg["dry_run"] = True
    engine.config.save(cfg)
    engine.balance_guard.check()
    assert api.calls and offers[123].active
    assert not save.called and not engine.ledger.holds()


def test_guard_works_with_paused_processing_and_paused_lot(guard_env):
    engine, _, _, offers, _ = guard_env
    cfg = engine.config.snapshot()
    cfg["enabled"] = False
    cfg["lots"]["123"]["enabled"] = False
    engine.config.save(cfg)
    engine.balance_guard.check()
    assert not offers[123].active


def test_guard_respects_disabled_cardinal_plugin(guard_env):
    engine, c, api, offers, _ = guard_env
    c.plugins[g.UUID].enabled = False
    engine.balance_guard.check()
    assert offers[123].active and not api.calls


def test_guard_blocks_cardinal_autorestore_but_allows_other_edits(guard_env):
    engine, c, _, offers, save = guard_env
    engine.balance_guard.check()
    fields = c.account.get_lot_fields(123)
    fields.active = True
    with pytest.raises(g.GGPError):
        c.account.save_lot(fields)
    assert not offers[123].active and save.call_count == 1
    fields.active = False
    fields.price = 15
    c.account.save_lot(fields)
    assert offers[123].price == 15
    offers[999] = NS(lot_id=999, active=False)
    c.account.save_lot(NS(lot_id=999, active=True))
    assert offers[999].active


def test_guard_failed_save_retries_and_only_reports_confirmed_success(guard_env):
    engine, c, _, offers, save = guard_env
    original = save.side_effect
    save.side_effect = RuntimeError("private-error")
    engine.balance_guard.check()
    assert offers[123].active and "123" in engine.ledger.holds()
    assert "Сняты с продажи" not in str(c.telegram.send_notification.call_args_list)
    assert "private-error" not in str(c.telegram.send_notification.call_args_list)
    save.side_effect = original
    engine.balance_guard.check()
    assert not offers[123].active
    assert "Сняты с продажи" in str(c.telegram.send_notification.call_args_list)


def test_guard_checks_remote_state_after_save(guard_env):
    engine, c, _, offers, save = guard_env
    save.side_effect = lambda fields: None
    engine.balance_guard.check()
    assert offers[123].active
    assert "не удалось" in str(c.telegram.send_notification.call_args_list).lower()
    assert "Сняты с продажи" not in str(c.telegram.send_notification.call_args_list)


def test_guard_hold_survives_restart_and_wrapper_is_restored(guard_env):
    engine, c, api, _, original = guard_env
    root = engine.config.path.parent
    engine.balance_guard.check()
    engine.close()
    assert c.account.save_lot is original
    restarted = g.Engine(c, root, api)
    try:
        restarted.balance_guard.install()
        with pytest.raises(g.GGPError):
            c.account.save_lot(NS(lot_id=123, active=True))
    finally:
        restarted.close()


def test_guard_recovery_requires_manual_restore_and_fresh_balance(guard_env):
    engine, _, api, offers, save = guard_env
    engine.balance_guard.check()
    api.overrides["check_balance"] = 20
    engine.balance_guard.check()
    assert not offers[123].active
    count = len(api.calls)
    restored, released, errors = engine.balance_guard.restore()
    assert offers[123].active and restored == ["123"] and not released and not errors
    assert len(api.calls) > count
    assert not engine.ledger.holds() and save.call_count == 2


def test_guard_restore_never_activates_previously_inactive_lot(guard_env):
    engine, _, api, offers, save = guard_env
    offers[123].active = False
    engine.balance_guard.check()
    assert not engine.ledger.holds()["123"]["disabled"]
    api.overrides["check_balance"] = 20
    restored, released, errors = engine.balance_guard.restore()
    assert not offers[123].active and not save.called
    assert not restored and released == ["123"] and not errors


@pytest.mark.parametrize("error", ["low", "network", "test", "disabled"])
def test_guard_does_not_restore_without_permission_and_balance(guard_env, error):
    engine, _, api, offers, _ = guard_env
    engine.balance_guard.check()
    if error == "network":
        api.fail = "check_balance"
    if error in {"test", "disabled"}:
        cfg = engine.config.snapshot()
        if error == "test":
            cfg["dry_run"] = True
        else:
            cfg["balance_guard"]["enabled"] = False
        engine.config.save(cfg)
        with pytest.raises(g.GGPError):
            engine.balance_guard.restore()
    else:
        restored, _, errors = engine.balance_guard.restore()
        assert not restored and errors
    assert not offers[123].active


def test_guard_currency_error_does_not_stop_other_currency(guard_env):
    engine, _, api, offers, _ = guard_env
    cfg = engine.config.snapshot()
    cfg["lots"]["124"] = dict(kind="stars", quantity="50", max_quantity="100", max_cost="5")
    engine.config.save(cfg)
    offers[124] = NS(lot_id=124, active=True)
    api.fail = "check_balance"
    engine.balance_guard.check()
    assert offers[123].active and not offers[124].active


def test_guard_checks_after_purchase(guard_env, env):
    engine, _, _, offers, _ = guard_env
    msg = prepare(env)
    engine.message(msg)
    done = threading.Event()
    engine.submit(done.set)
    assert done.wait(5)
    assert not offers[123].active


def test_guard_monitor_starts_once_and_stops(guard_env):
    engine, _, api, _, _ = guard_env
    checked = threading.Event()
    original = api.call
    def call(endpoint, payload=None, **kwargs):
        result = original(endpoint, payload, **kwargs)
        if endpoint == "check_balance":
            checked.set()
        return result
    api.call = call
    engine.balance_guard.start()
    thread = engine.balance_guard.thread
    engine.balance_guard.start()
    assert thread is engine.balance_guard.thread
    assert checked.wait(5)
    engine.balance_guard.stop()
    thread.join(2)
    assert not thread.is_alive()


def test_guard_check_queue_is_coalesced(guard_env):
    engine = guard_env[0]
    with engine.lock:
        for _ in range(10):
            engine.balance_guard.request()
    done = threading.Event()
    engine.submit(done.set)
    assert done.wait(5)
    assert sum(name == "check_balance" for name, _ in guard_env[2].calls) == 1


def test_legacy_configuration_gets_guard_disabled(tmp_path):
    legacy = {"enabled": True, "dry_run": False, "username": "u", "password": "p", "lots": {}}
    (tmp_path / "config.json").write_text(json.dumps(legacy))
    config = g.Config(tmp_path).snapshot()
    assert config["balance_guard"] == {"enabled": False, "USD": None, "TON": None}


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", True, "invalid"])
def test_guard_rejects_invalid_thresholds(tmp_path, value):
    cfg = g.Config(tmp_path)
    data = cfg.snapshot()
    data["balance_guard"]["USD"] = value
    with pytest.raises(g.GGPError):
        cfg.save(data)


def test_guard_threshold_ui_and_zero(ui):
    ui.click("guard")
    ui.click("guardset:USD")
    ui.type("10,5")
    assert "≤ 10.5 USD" in ui.text
    ui.click("guardset:TON")
    ui.type("0")
    assert "≤ 0 TON" in ui.text
    assert ui.engine.config.snapshot()["balance_guard"]["enabled"] is False


def test_guard_cannot_enable_without_threshold(ui):
    ui.click("guard")
    ui.click("guardon")
    assert "Сначала задайте" in ui.text
    assert not ui.engine.config.snapshot()["balance_guard"]["enabled"]


def test_guard_ui_manual_check_and_restore_confirmation(ui, guard_env):
    ui.click("guard")
    ui.click("guardcheck")
    assert not guard_env[3][123].active
    guard_env[2].overrides["check_balance"] = 50
    ui.click("guardrestoreask")
    assert not guard_env[3][123].active
    ui.click("guardrestore")
    assert guard_env[3][123].active
    assert "Возвращено в продажу: 1" in ui.text


def test_guard_ui_disable_requires_click_and_releases_autorestore(ui, guard_env):
    ui.click("guard")
    ui.click("guardcheck")
    ui.click("guardoffask")
    assert ui.engine.balance_guard.enabled()
    ui.click("guardoff")
    assert not ui.engine.balance_guard.enabled()
    guard_env[1].account.save_lot(NS(lot_id=123, active=True))
    assert guard_env[3][123].active


def test_guard_stops_if_plugin_disabled_during_funpay_read(guard_env):
    engine, c, _, offers, save = guard_env
    def read(lot_id):
        c.plugins[g.UUID].enabled = False
        return copy.deepcopy(offers[lot_id])
    c.account.get_lot_fields.side_effect = read
    engine.balance_guard.check()
    assert offers[123].active and not save.called


def test_guard_recovery_notification_is_not_repeated_for_varying_balances(guard_env):
    engine, c, api, _, _ = guard_env
    engine.balance_guard.check()
    for value in [20, 21, 22]:
        api.overrides["check_balance"] = value
        engine.balance_guard.check()
    notices = [str(call) for call in c.telegram.send_notification.call_args_list]
    assert sum("выше порога" in text for text in notices) == 1


def remote_profile(c, lots):
    return NS(id=c.account.id, get_sorted_lots=Mock(return_value=dict(lots)))


def test_new_funpay_lot_is_found_configured_and_processed_without_restart(ui, env):
    engine, event, c, api = env
    ui.click("lots:0")
    old_lot = event.lot_shortcut
    new_lot = copy.copy(old_lot)
    new_lot.id, new_lot.description = 456, "Brand new Steam lot"
    c.account.get_user.return_value = remote_profile(c, {123: old_lot, 456: new_lot})
    assert 456 not in c.profile.get_sorted_lots(1)
    ui.click("lotsrefresh")
    ui.click("lot:456")
    ui.click("setup:456")
    ui.click("kind:steam")
    ui.click("currency:USD")
    for value in ["1", "10", "10"]:
        ui.type(value)
    ui.click("save")
    assert "456" in engine.config.snapshot()["lots"]
    assert c.profile.get_sorted_lots(1)[456] is new_lot
    order = copy.copy(event.order)
    order.id, order.short_description = "AB456", new_lot.description
    c.account.get_order.return_value = order
    new_event = NS(order=order, lot_id=456, lot_shortcut=new_lot, delivered=False, config_section_obj=None)
    engine.new_order(new_event)
    engine.message(NS(text="!ggp AB456 test_login", author_id=2, chat_id=3))
    nonce = engine.ledger.get("AB456")["nonce"]
    engine.message(NS(text="!ggp AB456 да " + nonce, author_id=2, chat_id=3))
    assert engine.ledger.get("AB456")["state"] == "done"
    assert sum(name == "pay_order" for name, _ in api.calls) == 1


def test_lot_menu_automatically_refreshes_each_time_it_is_opened(ui, env):
    c = env[2]
    ui.click("lots:0")
    ui.click("home")
    new_lot = copy.copy(env[1].lot_shortcut)
    new_lot.id = 456
    c.account.get_user.return_value = remote_profile(c, {456: new_lot})
    before = c.account.get_user.call_count
    ui.click("lots:0")
    assert c.account.get_user.call_count == before + 1
    assert any(button.callback_data.endswith("lot:456") for button in ui.buttons)


def test_refresh_failure_keeps_previous_list_and_rules(ui, env):
    ui.click("lots:0")
    before = ui.engine.config.snapshot()
    env[2].account.get_user.side_effect = RuntimeError("private-cookie")
    ui.click("lotsrefresh")
    assert "Сохранён предыдущий список" in ui.text
    assert "private-cookie" not in ui.text
    assert any(button.callback_data.endswith("lot:123") for button in ui.buttons)
    assert ui.engine.config.snapshot() == before


def test_refresh_preserves_inactive_cardinal_history(env):
    engine, event, c, _ = env
    inactive = copy.copy(event.lot_shortcut)
    inactive.id = 999
    c.profile.get_sorted_lots.return_value[999] = inactive
    c.account.get_user.return_value = remote_profile(c, {123: event.lot_shortcut})
    assert 999 not in engine.refresh_lots(force=True)
    assert 999 in c.profile.get_sorted_lots(1)


def test_missing_configured_lot_is_not_called_deleted(ui, env):
    env[2].account.get_user.return_value = remote_profile(env[2], {})
    ui.click("lots:0")
    assert any("Не виден" in button.text for button in ui.buttons)
    ui.click("lot:123")
    assert "выключен, удалён или ещё не опубликован" in ui.text


def test_lot_disappears_during_wizard_without_overwriting_rule(ui, env):
    ui.click("lots:0")
    ui.click("lot:123")
    ui.click("setup:123")
    ui.click("kind:stars")
    for value in ["50", "100", "10"]:
        ui.type(value)
    before = ui.engine.config.snapshot()
    env[2].account.get_user.return_value = remote_profile(env[2], {})
    ui.click("save")
    assert "не виден" in ui.text
    assert ui.engine.config.snapshot() == before


def test_refresh_cache_for_pagination_and_force_button(env, monkeypatch):
    engine, _, c, _ = env
    now = [100.0]
    monkeypatch.setattr(g.time, "monotonic", lambda: now[0])
    engine.refresh_lots()
    engine.refresh_lots()
    assert c.account.get_user.call_count == 1
    engine.refresh_lots(force=True)
    assert c.account.get_user.call_count == 2
    now[0] += 31
    engine.refresh_lots()
    assert c.account.get_user.call_count == 3


def test_wrong_owner_refresh_cannot_replace_inventory(env):
    engine, _, c, _ = env
    original = engine.refresh_lots()
    c.account.get_user.return_value = NS(id=99, get_sorted_lots=Mock(return_value={}))
    with pytest.raises(g.GGPError):
        engine.refresh_lots(force=True)
    assert engine.current_lots() == original


def test_command_bind_finds_new_lot_from_funpay(env):
    engine, event, c, _ = env
    new_lot = copy.copy(event.lot_shortcut)
    new_lot.id = 456
    c.account.get_user.return_value = remote_profile(c, {456: new_lot})
    admin(engine, '/ggp_bind 456 {"kind":"steam","quantity":"1","max_quantity":"10","max_cost":"10"}')
    assert "456" in engine.config.snapshot()["lots"]


def test_command_lots_fetches_server_instead_of_startup_profile(env):
    engine, event, c, _ = env
    new_lot = copy.copy(event.lot_shortcut)
    new_lot.id, new_lot.description = 456, "Newly published"
    c.account.get_user.return_value = remote_profile(c, {456: new_lot})
    admin(engine, "/ggp_lots")
    assert "456: Newly published" in c.telegram.bot.send_message.call_args.args[1]


def test_readiness_uses_fresh_funpay_snapshot(ui, env):
    env[2].account.get_user.return_value = remote_profile(env[2], {})
    ui.click("diagnostics")
    assert "#123: лот не найден" in ui.text


def test_ledger_connections_are_closed_even_after_failure(tmp_path, monkeypatch):
    opened = []
    original = g.sqlite3.connect
    def connect(*args, **kwargs):
        db = original(*args, **kwargs)
        opened.append(db)
        return db
    monkeypatch.setattr(g.sqlite3, "connect", connect)
    ledger = g.Ledger(tmp_path)
    ledger.add({"id": "A", "state": "waiting"})
    ledger.get("A")
    with pytest.raises(RuntimeError):
        with ledger.connect() as db:
            db.execute("DELETE FROM orders")
            raise RuntimeError("rollback")
    assert ledger.get("A") is not None
    for db in opened:
        with pytest.raises(g.sqlite3.ProgrammingError):
            db.execute("SELECT 1")
