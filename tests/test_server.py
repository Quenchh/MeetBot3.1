"""server.py — WebSocket protokolü v2, yetkiler, Hub, REST uçları (+ main.py kablolaması)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import signal
import socket
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import server
from audio_manager import DownloadError, ResolveError
from config import VERSION
from fakes import MEET_LINK, FakeDownloader, ScriptedBot
from player import Player
from server import (
    ADMIN_TYPES,
    AUTH_GLOBAL_LIMIT,
    AUTH_LOCKOUT,
    AUTH_LOCKOUT_MAX,
    CONTROL_TYPES,
    FLOOD_LIMIT,
    MAX_AUTH_FAILURES,
    MAX_CONNECTIONS_PER_IP,
    MAX_FRAME_BYTES,
    MAX_INBOX,
    PUBLIC_TYPES,
    RATE_LIMIT,
    WS_MAX_SIZE,
    AuthLimiter,
    Client,
    Hub,
    clean_name,
    create_app,
    host_header_allowed,
    is_loopback,
    origin_allowed,
    parse_meet_link,
    permissions_for,
)

PASSWORD = "test-secret"


def build(settings, connected: bool = False) -> SimpleNamespace:
    bot = ScriptedBot(settings, connected=connected)
    downloader = FakeDownloader(settings.downloads_dir)
    hub = Hub()
    player = Player(settings, bot, downloader, hub.broadcast)
    bot.on_status = player.on_bot_status
    bot.on_track_ended = player.on_track_ended
    bot.on_progress = player.on_progress
    return SimpleNamespace(bot=bot, dl=downloader, hub=hub, player=player,
                           app=create_app(settings, player, bot, hub))


@pytest.fixture
def env(settings):
    return build(settings)


@pytest.fixture
def client(env):
    with TestClient(env.app) as test_client:
        yield test_client


class Session:
    """Küçük WebSocket istemcisi: gelen her mesajı `seen` içinde saklar."""

    def __init__(self, ws):
        self.ws = ws
        self.seen: list[dict] = []

    def recv(self) -> dict:
        message = self.ws.receive_json()
        self.seen.append(message)
        return message

    def until(self, predicate, limit: int = 200) -> dict:
        for _ in range(limit):
            message = self.recv()
            if predicate(message):
                return message
        raise AssertionError("beklenen mesaj gelmedi")

    def until_type(self, kind: str, **fields) -> dict:
        return self.until(lambda m: m.get("type") == kind and all(m.get(k) == v for k, v in fields.items()))

    def send(self, **message) -> None:
        self.ws.send_json(message)

    def request(self, kind: str, **fields) -> dict:
        rid = uuid.uuid4().hex[:8]
        self.ws.send_json({"type": kind, "rid": rid, **fields})
        return self.until(lambda m: m.get("type") == "ack" and m.get("rid") == rid)

    def error(self) -> str:
        return self.until_type("error")["message"]

    def expect(self, predicate) -> dict:
        """Eşleşen mesaj daha önce geldiyse onu, gelmediyse gelene kadar bekleyip döndürür."""
        for message in self.seen:
            if predicate(message):
                return message
        return self.until(predicate)

    def expect_type(self, kind: str, **fields) -> dict:
        return self.expect(lambda m: m.get("type") == kind and all(m.get(k) == v for k, v in fields.items()))

    def seen_last(self, kind: str) -> dict:
        """Daha önce (ör. bir ack beklenirken) gelmiş son `kind` mesajı."""
        return next(m for m in reversed(self.seen) if m.get("type") == kind)

    def hello(self, name: str = "Vedat", token: str | None = None) -> dict:
        message = {"type": "hello", "name": name}
        if token is not None:
            message["token"] = token
        self.ws.send_json(message)
        return self.until(lambda m: m.get("type") in ("welcome", "error"))

    def admin(self) -> str:
        ack = self.request("auth", password=PASSWORD)
        assert ack["ok"], ack
        return ack["data"]["token"]


def connect(client, **headers):
    return client.websocket_connect("/ws", headers=dict(headers))


# ──────────────────────────────────────────────────────────────
#  Saf yardımcılar
# ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("https://meet.google.com/abc-defg-hij", "https://meet.google.com/abc-defg-hij"),
    ("https://meet.google.com/ABC-DEFG-HIJ?authuser=0&hs=122", "https://meet.google.com/abc-defg-hij"),
    ("  link: https://meet.google.com/abc-defg-hij  ", "https://meet.google.com/abc-defg-hij"),
    ("https://meet.google.com/abc-defg-hijhttps://meet.google.com/abc-defg-hij", "https://meet.google.com/abc-defg-hij"),
    ("https://meet.google.com/abc-defg-hij/extra/junk#x", "https://meet.google.com/abc-defg-hij"),
    ("HTTPS://MEET.GOOGLE.COM/lookup/AbC_12-x?authuser=1", "https://meet.google.com/lookup/AbC_12-x"),
    ("http://meet.google.com/abc-defg-hij", None),
    ("https://meet.google.com/abc-defg", None),
    ("https://meet.google.com.evil.com/abc-defg-hij", None),
    ("https://meet.google.com/", None),
    ("abc-defg-hij", None),
])
def test_parse_meet_link(text, expected):
    assert parse_meet_link(text) == expected


@pytest.mark.parametrize("value,expected", [
    ("Vedat", "Vedat"), ("  Ve   dat \n", "Ve dat"), ("Ve\x00dat\u202e", "Vedat"), ("🎧 DJ", "🎧 DJ"),
    ("x" * 32, "x" * 32), ("x" * 33, None), ("", None), ("   ", None), (None, None), (42, None),
])
def test_clean_name(value, expected):
    assert clean_name(value) == expected


@pytest.mark.parametrize("headers,allowed", [
    ({"host": "localhost:8000"}, True),
    ({"host": "localhost:8000", "origin": "http://localhost:8000"}, True),
    ({"host": "192.168.1.5:8000", "origin": "http://192.168.1.5:8000"}, True),
    ({"host": "Example.com", "origin": "https://example.com:443"}, True),
    ({"host": "localhost:8000", "origin": "http://localhost:9000"}, False),
    ({"host": "localhost:8000", "origin": "http://evil.example"}, False),
    ({"host": "localhost:8000", "origin": "null"}, False),
    ({"host": "localhost:8000", "origin": "file://"}, False),
])
def test_origin_allowed(headers, allowed):
    assert origin_allowed(headers) is allowed


@pytest.mark.parametrize("host,allowed", [
    (None, True), ("", True),                                   # Host yok → tarayıcı değil
    ("localhost:8000", True), ("LOCALHOST", True), ("testserver", True), ("DESKTOP-AB12:8000", True),
    ("127.0.0.1:8000", True), ("192.168.1.5:8000", True), ("[::1]:8000", True), ("[fe80::1%25eth0]", True),
    ("meetbot.local:8000", True), ("pc.lan", True), ("meetbot.home.arpa", True), ("app.localhost", True),
    ("evil.test:8000", False), ("attacker.example", False), ("192.168.1.5.nip.io:8000", False),
    ("evil.test@127.0.0.1", False), ("[not-an-ip]:80", False), ("a b", False), (".", False),
])
def test_host_header_allowed_blocks_dns_rebinding_names(host, allowed):
    assert host_header_allowed(host) is allowed


def test_host_header_allowed_with_trusted_names():
    assert host_header_allowed("meetbot.example.com:8000", ("meetbot.example.com",))
    assert not host_header_allowed("evil.example.com:8000", ("meetbot.example.com",))
    assert host_header_allowed("evil.example.com", ("*",))  # "*" denetimi kapatır


def test_is_loopback():
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("::ffff:127.0.0.1")
    assert not is_loopback("192.168.1.5") and not is_loopback("testclient") and not is_loopback("?")


def test_permissions_for():
    assert permissions_for(False, False) == list(PUBLIC_TYPES)
    assert permissions_for(False, True) == list(PUBLIC_TYPES + CONTROL_TYPES)
    assert permissions_for(True, False) == list(PUBLIC_TYPES + CONTROL_TYPES + ADMIN_TYPES)
    assert "remove" in PUBLIC_TYPES  # misafir kendi eklediğini silebilir (sunucu sahipliği denetler)


# ──────────────────────────────────────────────────────────────
#  Hub
# ──────────────────────────────────────────────────────────────

class FakeSocket:
    def __init__(self, hang: bool = False, fail: bool = False):
        self.hang, self.fail = hang, fail
        self.sent: list[dict] = []
        self.close_code = None

    async def send_text(self, data: str) -> None:
        if self.hang:
            await asyncio.Event().wait()
        if self.fail:
            raise RuntimeError("bağlantı koptu")
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000) -> None:
        self.close_code = code


def hub_client(hub: Hub, name: str, **kwargs) -> Client:
    client = Client(FakeSocket(**kwargs))
    client.name = name
    hub.register(client)
    return client


async def test_hub_broadcast_drops_slow_and_dead_clients_quickly():
    hub = Hub(send_timeout=0.05)
    fast = hub_client(hub, "Zeynep")
    slow = hub_client(hub, "Yavaş", hang=True)
    dead = hub_client(hub, "Ölü", fail=True)
    other = hub_client(hub, "ali")
    started = time.monotonic()
    await hub.broadcast({"type": "queue", "queue": []})
    assert time.monotonic() - started < 1.0
    assert [c.name for c in hub.clients] == ["Zeynep", "ali"]
    assert slow.closed and dead.closed and slow.ws.close_code == 1011
    assert fast.ws.sent == [{"type": "queue", "queue": []},
                            {"type": "listeners", "listeners": ["ali", "Zeynep"], "count": 2}]
    assert other.ws.sent == fast.ws.sent
    await hub.broadcast({"type": "pong"})  # düşürülenlere artık gönderilmez
    assert slow.ws.sent == [] and dead.ws.sent == []


def test_hub_listeners_are_unique_and_sorted():
    hub = Hub()
    for name in ("vedat", "Ali", "vedat", "Ömer"):
        hub_client(hub, name)
    assert hub.listeners_msg() == {"type": "listeners", "listeners": ["Ali", "vedat", "Ömer"], "count": 3}


# ──────────────────────────────────────────────────────────────
#  REST
# ──────────────────────────────────────────────────────────────

def test_health_index_and_no_downloads_route(client, settings):
    assert client.get("/api/health").json() == {
        "ok": True, "version": VERSION, "bot_status": "disconnected", "queue_length": 0, "listeners": 0,
    }
    index = client.get("/")
    assert index.status_code == 200 and index.headers["cache-control"] == "no-cache"
    (settings.downloads_dir / "jNQXAC9IVRw.webm").write_bytes(b"secret audio")
    assert client.get("/downloads/jNQXAC9IVRw.webm").status_code == 404
    assert client.get("/docs").status_code == 404
    response = client.get("/api/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in response.headers


def test_health_counts_listeners_and_queue(client, env):
    with connect(client) as ws:
        session = Session(ws)
        session.hello("Vedat")
        assert session.request("add", query="song-a")["ok"]
        health = client.get("/api/health").json()
    assert health["listeners"] == 1 and health["queue_length"] == 1


# ──────────────────────────────────────────────────────────────
#  El sıkışma
# ──────────────────────────────────────────────────────────────

def test_hello_welcome_shape(client, settings):
    with connect(client) as ws:
        session = Session(ws)
        welcome = session.hello("  Vedat  ")
        assert welcome["type"] == "welcome"
        assert welcome["name"] == "Vedat" and welcome["is_admin"] is False
        assert welcome["version"] == VERSION and welcome["limits"] == settings.public_config
        assert welcome["permissions"] == permissions_for(False, settings.guest_controls)
        assert isinstance(welcome["client_id"], str) and welcome["client_id"]
        state = welcome["state"]
        assert set(state) == {"queue", "current", "playback", "volume", "mic_muted", "bot", "history", "listeners"}
        assert state["listeners"] == ["Vedat"]
        assert state["bot"] == {"status": "disconnected", "meet_link": None, "detail": None}
        assert session.until_type("listeners") == {"type": "listeners", "listeners": ["Vedat"], "count": 1}
        again = session.request("hello", name="Başka")
        assert again["ok"] is False and "zaten" in again["message"]


def test_state_request_returns_full_snapshot(client):
    with connect(client) as ws:
        session = Session(ws)
        session.hello()
        session.send(type="state")
        state = session.until_type("state")
        assert state["playback"] == {"state": "idle", "position": 0, "duration": 0, "repeat": "off"}
        assert state["listeners"] == ["Vedat"]


def test_messages_before_hello_are_rejected_but_socket_stays_open(client):
    with connect(client) as ws:
        session = Session(ws)
        session.send(type="ping")
        assert "hello" in session.error()
        ack = session.request("add", query="song-a")
        assert ack["ok"] is False and "hello" in ack["message"]
        for bad_name in ("", "   ", "x" * 33, 123):
            session.send(type="hello", name=bad_name)
            assert "İsim" in session.error()
        assert session.hello("Vedat")["type"] == "welcome"


def test_listeners_broadcast_on_join_and_leave(client):
    with connect(client) as ws1:
        first = Session(ws1)
        first.hello("Vedat")
        with connect(client) as ws2:
            Session(ws2).hello("Ali")
            assert first.until_type("listeners", count=2)["listeners"] == ["Ali", "Vedat"]
        assert first.until_type("listeners", count=1)["listeners"] == ["Vedat"]


# ──────────────────────────────────────────────────────────────
#  Hatalı girdi asla bağlantıyı kapatmaz
# ──────────────────────────────────────────────────────────────

def test_bad_input_never_closes_the_socket(client):
    with connect(client) as ws:
        session = Session(ws)
        session.hello()
        cases = [
            ("not json", "Geçersiz JSON"),
            ("[1, 2]", "JSON nesnesi"),
            ("{}", "type"),
            ('{"type": 5}', "type"),
            ('{"type": "nope"}', "Bilinmeyen mesaj türü"),
            ('{"type": "remove", "id": "abc"}', "'id' bir tam sayı"),
            ('{"type": "remove", "id": true}', "'id' bir tam sayı"),
            ('{"type": "move", "id": 1, "index": "x"}', "'index' bir tam sayı"),
            ('{"type": "seek", "position": "10"}', "'position' bir sayı"),
            ('{"type": "seek", "position": -5}', "negatif"),
            ('{"type": "volume", "target": "music", "value": "50"}', "'value' bir tam sayı"),
            ('{"type": "volume", "target": "music", "value": 500}', "0–100"),
            ('{"type": "add", "query": 123}', "'query'"),
            ('{"type": "add", "query": "' + "x" * 600 + '"}', "'query'"),
            ('{"type": "ping", "rid": 123}', "'rid'"),
            ('{"type": "ping", "rid": "' + "r" * 65 + '"}', "'rid'"),
            ("[" * 5000, "Geçersiz JSON"),
            ('{"type": "ping", "pad": "' + "x" * 20_000 + '"}', "çok büyük"),
            ('{"type": "remove", "id": 99999}', "bulunamadı"),
        ]
        for raw, expected in cases:
            ws.send_text(raw)
            message = session.error()
            assert expected in message, (raw[:40], message)
        ws.send_bytes(b"\x00\x01binary")
        assert "metin" in session.error()
        session.send(type="ping")
        session.until_type("pong")


def test_rate_limit_replies_with_errors_instead_of_disconnecting(client):
    with connect(client) as ws:
        session = Session(ws)
        session.hello()  # 1. mesaj
        for _ in range(35):
            session.send(type="state")
        # Hata yanıtı okuma döngüsünden hemen, state yanıtı işçiden gelir → sıra değil sayı denetlenir
        replies = [session.until(lambda m: m["type"] in ("state", "error")) for _ in range(35)]
        errors = [r for r in replies if r["type"] == "error"]
        assert len(replies) - len(errors) == RATE_LIMIT - 1 and len(errors) == 36 - RATE_LIMIT
        assert all("yavaşla" in e["message"] for e in errors)
        session.send(type="state")  # hâlâ açık (hâlâ sınırda)
        assert "yavaşla" in session.error()
        # rid'li istek sınıra takılınca da yanıtını (ack) alır; arayüz beklemede kalmaz
        limited = session.request("state")
        assert limited["ok"] is False and "yavaşla" in limited["message"]
        # Kalp atışı sınıra takılmaz: pong gelmezse arayüz sağlam bağlantıyı koparırdı
        session.send(type="ping")
        assert session.until(lambda m: m["type"] in ("pong", "error"))["type"] == "pong"
        assert session.request("ping")["ok"] is True


def test_heartbeat_ping_is_answered_after_busy_volume_drag(client):
    """Bulgu: 29 ses mesajından sonra gelen kalp atışı 'yavaşla' hatası alıyordu (pong yok)."""
    with connect(client) as ws:
        session = Session(ws)
        session.hello()
        for value in range(RATE_LIMIT - 1):
            session.send(type="volume", target="music", value=value)
        session.send(type="ping")
        reply = session.until(lambda m: m["type"] in ("pong", "error"))
        assert reply == {"type": "pong"}


def test_oversized_and_binary_frames_count_toward_the_rate_limit(client):
    with connect(client) as ws:
        session = Session(ws)
        session.hello()  # 1. mesaj
        big = json.dumps({"type": "state", "pad": "x" * (MAX_FRAME_BYTES + 10)})
        for i in range(RATE_LIMIT - 1):
            if i % 2:
                ws.send_bytes(b"\x00binary")
            else:
                ws.send_text(big)
        messages = [session.error() for _ in range(RATE_LIMIT - 1)]
        assert all("çok büyük" in m or "metin" in m for m in messages)
        # Bütçe büyük/ikili çerçevelerle bitti → normal komut da sınıra takılır
        limited = session.request("state")
        assert limited["ok"] is False and "yavaşla" in limited["message"]


def test_message_flood_closes_the_connection_with_1008(client):
    with connect(client) as ws:
        session = Session(ws)
        session.hello()
        for _ in range(FLOOD_LIMIT + 5):
            ws.send_text("not json")
        # Kapatılmasaydı tam FLOOD_LIMIT + 5 hata gelirdi; döngü sınırlı → gerilemede asılı kalmaz
        with pytest.raises(WebSocketDisconnect) as exc_info:
            for _ in range(FLOOD_LIMIT + 5):
                session.recv()
        assert exc_info.value.code == 1008
    # Diğer istemciler etkilenmez
    with connect(client) as ws:
        assert Session(ws).hello()["type"] == "welcome"


def test_uvicorn_frame_limit_is_small():
    """1 MB'lık çerçeveler olay döngüsünü herkes için kilitliyordu; sınır 16 KB'ın küçük bir katı."""
    import main

    assert main.WS_MAX_SIZE == WS_MAX_SIZE
    assert MAX_FRAME_BYTES < WS_MAX_SIZE <= 64 * 1024


def test_hello_with_rid_is_acked_after_welcome(client):
    """Arayüz hello'yu rid ile gönderir ve ack bekler (isim reddi → ok:false)."""
    with connect(client) as ws:
        session = Session(ws)
        rejected = session.request("hello", name="   ")
        assert rejected["ok"] is False and "İsim" in rejected["message"]
        accepted = session.request("hello", name="Vedat")
        assert accepted == {"type": "ack", "rid": accepted["rid"], "ok": True, "message": None, "data": None}
        assert any(m["type"] == "welcome" for m in session.seen[:-1])  # welcome, ack'ten önce


# ──────────────────────────────────────────────────────────────
#  Komut sırası: yavaş komut okuma döngüsünü (ve kalp atışını) bloklamaz
# ──────────────────────────────────────────────────────────────

def slow_leave(env, delay: float) -> None:
    original = env.bot.leave

    async def leave():
        await asyncio.sleep(delay)
        await original()

    env.bot.leave = leave


def test_ping_is_answered_while_a_slow_command_runs_and_commands_keep_their_order(settings):
    env = build(settings, connected=True)
    slow_leave(env, 0.5)
    with TestClient(env.app) as test_client, connect(test_client) as ws:
        session = Session(ws)
        session.hello()
        session.admin()
        session.send(type="leave_meet", rid="ayril")
        session.send(type="state", rid="durum")
        session.send(type="ping")
        assert session.until(lambda m: m["type"] in ("pong", "ack"))["type"] == "pong"
        assert [session.until_type("ack")["rid"] for _ in range(2)] == ["ayril", "durum"]
        assert session.seen_last("state")["bot"]["status"] == "disconnected"  # ayrılmadan SONRA işlendi


def test_commands_beyond_the_inbox_limit_are_refused(settings):
    env = build(settings, connected=True)
    slow_leave(env, 1.0)
    with TestClient(env.app) as test_client, connect(test_client) as ws:
        session = Session(ws)
        session.hello()
        session.admin()
        session.send(type="leave_meet", rid="ayril")  # işçi bununla meşgul
        for i in range(MAX_INBOX + 1):
            session.send(type="state", rid=f"s{i}")
        acks: dict[str, dict] = {}
        while len(acks) < MAX_INBOX + 2:
            ack = session.until_type("ack")
            acks[ack["rid"]] = ack
        refused = [rid for rid, ack in acks.items() if not ack["ok"]]
        assert refused == [f"s{MAX_INBOX}"]
        assert "meşgul" in acks[f"s{MAX_INBOX}"]["message"]


def test_commands_sent_right_before_disconnect_still_run(settings):
    env = build(settings, connected=True)
    slow_leave(env, 0.3)
    with TestClient(env.app) as test_client:
        with connect(test_client) as ws:
            session = Session(ws)
            session.hello()
            session.admin()
            session.send(type="leave_meet")
        deadline = time.monotonic() + 3
        while env.bot.status != "disconnected" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert env.bot.status == "disconnected"
        assert test_client.get("/api/health").json()["listeners"] == 0


def test_foreign_origin_is_rejected_with_1008(client):
    for origin in ("http://evil.example", "null"):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with connect(client, origin=origin) as ws:
                ws.receive_json()
        assert exc_info.value.code == 1008
    with connect(client, origin="http://testserver") as ws:
        assert Session(ws).hello()["type"] == "welcome"


def test_dns_rebinding_host_is_rejected_on_ws_and_http(settings):
    """Rebinding sonrası Origin ile Host ikisi de saldırganın adıdır: Origin==Host denetimi yetmez."""
    env = build(settings)
    with TestClient(env.app) as test_client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with connect(test_client, host="evil.test:8000", origin="http://evil.test:8000") as ws:
                ws.receive_json()
        assert exc_info.value.code == 1008
        assert test_client.get("/", headers={"host": "evil.test:8000"}).status_code == 400
        assert test_client.get("/api/health", headers={"host": "attacker.example"}).status_code == 400
        for host in ("192.168.1.5:8000", "localhost:8000", "meetbot.local:8000"):
            assert test_client.get("/api/health", headers={"host": host}).status_code == 200
            with connect(test_client, host=host, origin=f"http://{host}") as ws:
                assert Session(ws).hello()["type"] == "welcome"


def test_trusted_hostnames_setting_is_parsed(tmp_path):
    from config import load_settings

    loaded = load_settings(env={"MEETBOT_TRUSTED_HOSTNAMES": " MeetBot.Example.com , radyo.ev.net ,"},
                           dotenv_path=tmp_path / "yok.env")
    assert loaded.trusted_hostnames == ("meetbot.example.com", "radyo.ev.net")
    assert load_settings(env={}, dotenv_path=tmp_path / "yok.env").trusted_hostnames == ()


def test_trusted_hostnames_setting_allows_a_reverse_proxy_name(settings):
    settings.trusted_hostnames = ("meetbot.example.com",)
    env = build(settings)
    with TestClient(env.app) as test_client:
        headers = {"host": "meetbot.example.com", "origin": "https://meetbot.example.com"}
        assert test_client.get("/api/health", headers={"host": headers["host"]}).status_code == 200
        with connect(test_client, **headers) as ws:
            assert Session(ws).hello()["type"] == "welcome"


# ──────────────────────────────────────────────────────────────
#  Bağlantı sınırları: adres başına, hello süresi
# ──────────────────────────────────────────────────────────────

def with_client_address(app):
    """Test ASGI sarmalayıcısı: 'x-test-client' başlığı bağlantının uzak adresi olur
    (tek TestClient / tek olay döngüsüyle farklı adreslerden bağlanmak için)."""
    async def wrapper(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            for key, value in scope.get("headers", []):
                if key == b"x-test-client":
                    scope = {**scope, "client": (value.decode(), 50000)}
        await app(scope, receive, send)
    return wrapper


def connect_from(client, address: str, **headers):
    return connect(client, **{"x-test-client": address}, **headers)


def test_one_address_cannot_fill_the_connection_limit(settings):
    """Bulgu: tek bir misafir 100 boş soket açıp herkesi (yönetici dahil) dışarıda bırakabiliyordu."""
    env = build(settings)
    with TestClient(with_client_address(env.app)) as test_client, contextlib.ExitStack() as stack:
        for _ in range(MAX_CONNECTIONS_PER_IP):
            stack.enter_context(connect_from(test_client, "192.168.1.66"))
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with connect_from(test_client, "192.168.1.66") as ws:
                ws.receive_json()
        assert exc_info.value.code == 1013
        # Başka bir adres (ör. yönetici) yine bağlanabilir
        with connect_from(test_client, "192.168.1.20") as ws:
            assert Session(ws).hello("Yönetici")["type"] == "welcome"
        # Yerel makine (döngü adresi; ör. yerel ters vekil) adres sınırına takılmaz
        with contextlib.ExitStack() as local:
            for _ in range(MAX_CONNECTIONS_PER_IP + 1):
                local.enter_context(connect_from(test_client, "127.0.0.1"))
        stack.close()  # kapanan soketler sayaçtan düşer
        with connect_from(test_client, "192.168.1.66") as ws:
            assert Session(ws).hello()["type"] == "welcome"


def test_socket_without_hello_is_closed_after_the_deadline(settings, monkeypatch):
    monkeypatch.setattr(server, "HELLO_TIMEOUT", 0.3)
    env = build(settings)
    with TestClient(env.app) as test_client:
        with connect(test_client) as idle:
            started = time.monotonic()
            with pytest.raises(WebSocketDisconnect) as exc_info:
                idle.receive_json()
            assert exc_info.value.code == 1008 and time.monotonic() - started < 5
        with connect(test_client) as ws:  # hello yapan bağlantı süreden sonra da açık kalır
            session = Session(ws)
            session.hello()
            time.sleep(0.5)
            session.send(type="ping")
            session.until_type("pong")


# ──────────────────────────────────────────────────────────────
#  Yetkiler
# ──────────────────────────────────────────────────────────────

REQUEST_FIELDS = {
    "move": {"id": 424242, "index": 0},
    "play_now": {"id": 424242},
    "seek": {"position": 1},
    "repeat": {"mode": "all"},
    "volume": {"target": "music", "value": 55},
    "mic": {"muted": True},
    "join_meet": {"link": MEET_LINK},
}


@pytest.mark.parametrize("guest_controls,as_admin", [(True, False), (False, False), (False, True), (True, True)])
def test_permission_matrix(settings, guest_controls, as_admin):
    settings.guest_controls = guest_controls
    env = build(settings)
    with TestClient(env.app) as test_client, connect(test_client) as ws:
        session = Session(ws)
        welcome = session.hello("Misafir")
        if as_admin:
            session.admin()
            session_msg = session.seen_last("session")
            assert session_msg["is_admin"] is True
            assert session_msg["permissions"] == permissions_for(True, guest_controls)
        else:
            assert welcome["permissions"] == permissions_for(False, guest_controls)
        allowed = set(permissions_for(as_admin, guest_controls))
        for kind in CONTROL_TYPES + ADMIN_TYPES:
            ack = session.request(kind, **REQUEST_FIELDS.get(kind, {}))
            denied = ack["ok"] is False and "yetki" in (ack["message"] or "")
            assert denied == (kind not in allowed), (kind, ack)
        if as_admin:
            assert env.bot.music_volume == 55 and env.player.repeat == "all"


def test_guest_without_controls_can_remove_only_own_tracks(settings):
    settings.guest_controls = False
    env = build(settings)
    with TestClient(env.app) as test_client, connect(test_client) as ws_a, connect(test_client) as ws_b:
        ali, vedat = Session(ws_a), Session(ws_b)
        ali.hello("Ali")
        vedat.hello("Vedat")
        ack = ali.request("add", query="song-a")
        assert ack["ok"] and ack["data"]["tracks"][0]["added_by"] == "Ali"
        track_id = ack["data"]["tracks"][0]["id"]
        denied = vedat.request("remove", id=track_id)
        assert denied["ok"] is False and "kendi eklediğin" in denied["message"]
        removed = ali.request("remove", id=track_id)
        assert removed["ok"] is True
        assert env.player.queue == []


# ──────────────────────────────────────────────────────────────
#  Yönetici oturumu
# ──────────────────────────────────────────────────────────────

def test_admin_token_is_reused_on_reconnect_until_logout(client):
    with connect(client) as ws1:
        first = Session(ws1)
        first.hello("Vedat")
        wrong = first.request("auth", password="yanlış")
        assert wrong["ok"] is False and wrong["message"] == "Şifre yanlış"
        token = first.admin()
        assert len(token) >= 32
        assert first.seen_last("session")["is_admin"] is True

        with connect(client) as ws2:
            second = Session(ws2)
            welcome = second.hello("Vedat", token=token)
            assert welcome["is_admin"] is True
            assert welcome["permissions"] == permissions_for(True, True)
            assert second.request("logout")["ok"]
            assert second.seen_last("session")["is_admin"] is False
        # Aynı jetonu kullanan diğer sekme de yetkisini kaybeder
        assert first.until_type("session")["is_admin"] is False

    with connect(client) as ws3:
        assert Session(ws3).hello("Vedat", token=token)["is_admin"] is False
    with connect(client) as ws4:
        assert Session(ws4).hello("Vedat", token="uydurma-jeton")["is_admin"] is False


def test_auth_lockout_after_five_failures(client):
    with connect(client) as ws:
        session = Session(ws)
        session.hello()
        messages = [session.request("auth", password=f"yanlış-{i}")["message"] for i in range(5)]
        assert messages[:4] == ["Şifre yanlış"] * 4
        assert "Çok fazla hatalı deneme" in messages[4]
        locked = session.request("auth", password=PASSWORD)
        assert locked["ok"] is False and "sn sonra" in locked["message"]
        assert session.request("auth", password={"not": "a string"})["ok"] is False


def test_auth_lockout_survives_reconnect_and_is_per_address(settings):
    """Bulgu: kilit bağlantı başınaydı; her 5 denemede yeniden bağlanarak sn'de ~135 şifre denenebiliyordu."""
    env = build(settings)
    with TestClient(with_client_address(env.app)) as test_client:
        for attempt in range(3):  # her denemede YENİ bağlantı: sayaç sıfırlanmamalı
            with connect_from(test_client, "192.168.1.66") as ws:
                session = Session(ws)
                session.hello("Saldırgan")
                for i in range(2):
                    session.request("auth", password=f"tahmin-{attempt}-{i}")
        with connect_from(test_client, "192.168.1.66") as ws:
            session = Session(ws)
            session.hello("Saldırgan")
            locked = session.request("auth", password=PASSWORD)  # doğru şifre bile reddedilir
            assert locked["ok"] is False and "Çok fazla hatalı deneme" in locked["message"]
        with connect_from(test_client, "192.168.1.20") as ws:  # yönetici başka adreste: etkilenmez
            session = Session(ws)
            session.hello("Yönetici")
            assert session.request("auth", password=PASSWORD)["ok"] is True


def test_auth_limiter_backoff_doubles_and_success_resets():
    limiter = AuthLimiter()
    now = 1000.0
    for i in range(1, MAX_AUTH_FAILURES):
        assert limiter.failure("10.0.0.5", now) == (i, 0.0)
    assert limiter.failure("10.0.0.5", now) == (MAX_AUTH_FAILURES, AUTH_LOCKOUT)
    assert limiter.wait_time("10.0.0.5", now + 1) == pytest.approx(AUTH_LOCKOUT - 1)
    assert limiter.wait_time("10.0.0.6", now + 1) == 0  # başka adres serbest
    now += AUTH_LOCKOUT
    assert limiter.wait_time("10.0.0.5", now) == 0
    for _ in range(MAX_AUTH_FAILURES):
        _, lockout = limiter.failure("10.0.0.5", now)
    assert lockout == 2 * AUTH_LOCKOUT  # tekrarlayan kilit ikiye katlanır
    for _ in range(20):
        now += AUTH_LOCKOUT_MAX
        for _ in range(MAX_AUTH_FAILURES):
            _, lockout = limiter.failure("10.0.0.5", now)
        assert lockout <= AUTH_LOCKOUT_MAX
    limiter.success("10.0.0.5")
    assert limiter.wait_time("10.0.0.5", now) == 0
    assert limiter.failure("10.0.0.5", now) == (1, 0.0)


def test_auth_limiter_global_cap_stops_many_address_guessing():
    limiter = AuthLimiter()
    now = 50.0
    for i in range(AUTH_GLOBAL_LIMIT):
        limiter.failure(f"10.0.1.{i}", now)  # her adres sınırın altında
    assert limiter.wait_time("10.0.2.1", now + 1) == pytest.approx(59.0)
    assert limiter.wait_time("10.0.2.1", now + 60) == 0


# ──────────────────────────────────────────────────────────────
#  Ekleme / oynatma akışı
# ──────────────────────────────────────────────────────────────

def test_add_runs_in_background_and_socket_keeps_answering(client, env):
    env.dl.resolve_delay = 0.3
    with connect(client) as ws:
        session = Session(ws)
        session.hello()
        session.send(type="add", query="song-a", rid="ekle")
        session.send(type="ping")
        first = session.until(lambda m: m["type"] in ("pong", "ack"))
        assert first["type"] == "pong"
        ack = session.until_type("ack", rid="ekle")
        assert ack["ok"] and ack["message"] == "🎵 Şarkı song-a kuyruğa eklendi"
        assert session.seen_last("queue")["queue"][0]["title"] == "Şarkı song-a"


def test_pending_add_limit(client, env):
    env.dl.resolve_delay = 0.5
    with connect(client) as ws:
        session = Session(ws)
        session.hello()
        for i in range(3):
            session.send(type="add", query=f"song-{i}0", rid=f"a{i}")
        refused = session.request("add", query="song-30")
        assert refused["ok"] is False and "bitmesini bekle" in refused["message"]
        pending = {f"a{i}" for i in range(3)}
        while pending:
            ack = session.until(lambda m: m.get("type") == "ack" and m.get("rid") in pending)
            assert ack["ok"], ack
            pending.discard(ack["rid"])


def test_add_errors_come_back_as_ack(client, env):
    with connect(client) as ws:
        session = Session(ws)
        session.hello()
        ack = session.request("add", query="https://evil.example/x")
        assert ack["ok"] is True  # FakeDownloader her şeyi kabul eder; gerçek kontrol audio_manager'da
        env.dl.resolve_error = ResolveError("Sonuç bulunamadı")
        ack = session.request("add", query="bulunmayan")
        assert ack == {"type": "ack", "rid": ack["rid"], "ok": False, "message": "Sonuç bulunamadı", "data": None}


def test_full_flow_broadcasts_and_never_leaks_paths(settings):
    env = build(settings, connected=True)
    env.dl.download_errors["song-c"] = DownloadError("Video kullanılamıyor")

    def playing(video_id):
        return lambda m: (m["type"] == "playback" and m["state"] == "playing"
                          and m["current"]["video_id"] == video_id)

    with TestClient(env.app) as test_client, connect(test_client) as ws:
        session = Session(ws)
        session.hello()
        session.admin()
        for query in ("song-a", "song-b", "song-c"):
            assert session.request("add", query=query)["ok"]
        session.expect(playing("song-a"))
        assert session.request("skip")["ok"]
        session.expect(playing("song-b"))
        assert session.expect(lambda m: m["type"] == "history")["history"][0]["video_id"] == "song-a"
        assert session.request("volume", target="mic", value=30)["ok"]
        assert session.seen_last("volume") == {"type": "volume", "music": 80, "mic": 30}
        assert session.request("mic", muted=True)["ok"]
        assert session.seen_last("mic") == {"type": "mic", "muted": True}
        assert session.request("repeat", mode="one")["ok"]
        assert session.request("state")["ok"]
        state = session.seen_last("state")
        assert state["current"]["video_id"] == "song-b" and state["playback"]["repeat"] == "one"
        assert state["queue"][0]["status"] == "error"
        assert session.request("stop")["ok"]
        assert session.seen_last("playback")["state"] == "idle"
        assert {"type": "notice", "level": "success", "message": "🎵 Vedat: Şarkı song-a kuyruğa eklendi"} in session.seen

    dumped = json.dumps(session.seen, ensure_ascii=False)
    assert "file_path" not in dumped
    assert json.dumps(str(settings.downloads_dir))[1:-1] not in dumped
    assert re.search(r"[A-Za-z]:\\\\", dumped) is None, "mutlak Windows yolu sızdı"
    assert re.search(r'"/(?:home|Users|tmp|var)/', dumped) is None, "mutlak POSIX yolu sızdı"
    assert "Video kullanılamıyor" not in dumped  # iç hata metni (Track.error) istemciye gitmez


def test_join_and_leave_meet(client, env):
    with connect(client) as ws:
        session = Session(ws)
        session.hello()
        session.admin()
        bad = session.request("join_meet", link="https://evil.example/abc-defg-hij")
        assert bad["ok"] is False and "Geçersiz Meet" in bad["message"]
        ack = session.request("join_meet", link="Link: https://meet.google.com/ABC-DEFG-HIJ?authuser=0 teşekkürler")
        assert ack["ok"] and ack["message"] == "Toplantıya bağlanılıyor…"
        connected = session.expect_type("bot", status="connected")
        assert connected["meet_link"] == MEET_LINK
        same = session.request("join_meet", link=MEET_LINK + "?pli=1")
        assert same["ok"] and same["message"] == "Bot zaten bu toplantıda"
        left = session.request("leave_meet")
        assert left["ok"] and left["message"] == "Bot toplantıdan ayrıldı"
        # Bot durumu, ack'ten ÖNCE yayınlanır (oynatıcı kilidini beklemez)
        assert session.expect_type("bot", status="disconnected")["detail"] == "Toplantıdan ayrıldı"
        again = session.request("leave_meet")
        assert again["ok"] is False and "zaten" in again["message"]


def test_leave_meet_while_connecting_cancels_join(client, env):
    env.bot.join_delay = 30
    with connect(client) as ws:
        session = Session(ws)
        session.hello()
        session.admin()
        assert session.request("join_meet", link=MEET_LINK)["ok"]
        session.expect_type("bot", status="connecting")
        cancelled = session.request("leave_meet")
        assert cancelled["ok"] and cancelled["message"] == "Katılma iptal edildi"
        assert session.expect_type("bot", status="disconnected")["detail"] == "Katılma iptal edildi"
        session.send(type="ping")
        session.until_type("pong")
        assert env.bot.status == "disconnected"
        assert not any(m.get("type") == "bot" and m.get("status") == "connected" for m in session.seen)


def test_lifespan_shutdown_stops_player_then_bot_even_if_one_fails(settings):
    env = build(settings)
    order: list[str] = []

    async def broken_player_shutdown():
        order.append("player")
        raise RuntimeError("bozuk")

    async def bot_shutdown():
        order.append("bot")

    env.player.shutdown = broken_player_shutdown
    env.bot.shutdown = bot_shutdown
    with TestClient(env.app):
        pass
    assert order == ["player", "bot"]


# ──────────────────────────────────────────────────────────────
#  main.py kablolaması
# ──────────────────────────────────────────────────────────────

def test_main_fake_bot_wiring(settings):
    import main
    from fake_bot import FakeBot

    app, player, bot, downloader = main.build_application(settings, fake_bot=True)
    assert isinstance(bot, FakeBot)
    assert bot.on_status == player.on_bot_status
    assert bot.on_track_ended == player.on_track_ended
    assert bot.on_progress == player.on_progress
    assert downloader.downloads_dir == settings.downloads_dir.resolve()
    with TestClient(app) as test_client:
        assert test_client.get("/api/health").json()["bot_status"] == "disconnected"


def test_main_cli_arguments():
    import main

    args = main.parse_args(["--host", "127.0.0.1", "--port", "8799", "--fake-bot"])
    assert (args.host, args.port, args.fake_bot, args.doctor) == ("127.0.0.1", 8799, True, False)
    assert main.parse_args(["--doctor"]).doctor is True


@pytest.mark.skipif(not hasattr(signal, "SIGBREAK"), reason="Windows'a özgü (Ctrl+Break)")
def test_ctrl_break_ends_like_ctrl_c():
    """uvicorn kapanıştan sonra SIGBREAK'i yeniden yükseltir: varsayılan işleyici süreci
    3 koduyla öldürürdü; KeyboardInterrupt olunca main() temiz biter."""
    import main

    previous = signal.getsignal(signal.SIGBREAK)
    try:
        main.install_break_handler()
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGBREAK)
    finally:
        signal.signal(signal.SIGBREAK, previous)


@pytest.mark.parametrize("host,expected", [
    ("0.0.0.0", "localhost"), ("::", "localhost"), ("127.0.0.1", "localhost"), ("::1", "localhost"),
    ("192.168.1.5", "192.168.1.5"), ("fe80::1", "[fe80::1]"), ("meetbot.local", "meetbot.local"),
])
def test_banner_url_host(host, expected):
    import main

    assert main.url_host(host) == expected


def test_doctor_checks_the_configured_js_runtime(monkeypatch):
    import main

    monkeypatch.setattr(main.shutil, "which", lambda name: None)
    assert main._check_js_runtime("none") is None  # bilerek kapatıldı → denetlenmez
    ok, label, detail = main._check_js_runtime("auto")
    assert (ok, label) == (False, "Node.js") and "nodejs.org" in detail
    assert main._check_js_runtime("deno")[:2] == (False, "Deno")
    monkeypatch.setattr(main.shutil, "which", lambda name: f"C:/bin/{name}.exe")
    monkeypatch.setattr(main, "_command_version", lambda command: (True, "v24.19.0"))
    assert main._check_js_runtime("auto") == (True, "Node.js", "v24.19.0 (C:/bin/node.exe)")


@pytest.mark.parametrize("mode,output,ok", [
    ("auto", "v20.11.1", False),     # yt-dlp Node < 22'yi sessizce yok sayar
    ("node", "v18.20.4", False),
    ("auto", "v22.0.0", True),
    ("deno", "deno 2.3.1 (stable, release, x86_64-pc-windows-msvc)", True),
    ("deno", "deno 1.46.3 (stable)", False),
    ("bun", "1.2.11", True),
    ("bun", "1.1.0", False),
    ("auto", "garip çıktı", False),
])
def test_doctor_requires_the_js_runtime_version_ytdlp_supports(monkeypatch, mode, output, ok):
    import main

    monkeypatch.setattr(main.shutil, "which", lambda name: f"C:/bin/{name}.exe")
    monkeypatch.setattr(main, "_command_version", lambda command: (True, output))
    result_ok, _, detail = main._check_js_runtime(mode)
    assert result_ok is ok
    assert output in detail
    if not ok:
        assert "en az" in detail and "güncelleyin" in detail


def test_banner_warns_about_a_short_admin_password(settings, capsys):
    import main

    settings.admin_password, settings.admin_password_generated = "1234", False
    main.print_banner(settings, fake_bot=False)
    out = capsys.readouterr().out
    assert "kısa (4 karakter)" in out and "1234" not in out
    settings.admin_password = "uzun-ve-tahmin-edilemez-bir-sifre"
    main.print_banner(settings, fake_bot=False)
    assert "kısa" not in capsys.readouterr().out


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_second_ctrl_c_during_graceful_wait_still_shuts_down_player_and_bot(settings, monkeypatch):
    """Bulgu: uvicorn ikinci Ctrl+C'de (force_exit) lifespan'i atlıyordu → bot toplantıda kalıyordu."""
    import websockets

    import main

    settings.host, settings.port = "127.0.0.1", _free_port()
    application = main.build_application(settings, fake_bot=True)
    calls: list[str] = []
    for obj, label in ((application.player, "player"), (application.bot, "bot")):
        original = obj.shutdown

        async def tracked(original=original, label=label):
            calls.append(label)
            await original()

        obj.shutdown = tracked
    uv_server = main.make_server(settings, application.app)
    uv_server.config.timeout_graceful_shutdown = 2  # testi kısaltır (main.py: 10 sn)
    # Gerçek sinyal işleyicileri kurulmasın / sonunda sinyal yeniden yükseltilmesin (pytest süreci)
    monkeypatch.setattr(uv_server, "capture_signals", contextlib.nullcontext)
    serve_task = asyncio.ensure_future(uv_server.serve())
    ws = None
    try:
        await wait_for_condition(lambda: uv_server.started)
        application.bot.join_delay = 0.0
        application.bot.request_join(MEET_LINK)
        await wait_for_condition(lambda: application.bot.status == "connected")
        ws = await websockets.connect(f"ws://127.0.0.1:{settings.port}/ws")
        await ws.send(json.dumps({"type": "hello", "name": "Vedat"}))
        await ws.recv()
        await application.player._lock.acquire()  # uzun süren bir komut (ör. bot.play yüklemesi) sürüyor
        await ws.send(json.dumps({"type": "skip", "rid": "1"}))
        await asyncio.sleep(0.3)
        uv_server.handle_exit(signal.SIGINT, None)   # Ctrl+C → bağlantıların kapanması bekleniyor
        await asyncio.sleep(0.5)
        uv_server.handle_exit(signal.SIGINT, None)   # sabırsız ikinci Ctrl+C → force_exit
        await asyncio.wait_for(serve_task, 20)
        assert uv_server.force_exit
        assert calls == ["player", "bot"]
        assert application.bot.status == "disconnected" and application.bot.meet_link is None
        await application.app.state.shutdown()  # tek seferlik: ikinci çağrı bir şey yapmaz
        assert calls == ["player", "bot"]
    finally:
        if application.player._lock.locked():
            application.player._lock.release()
        if ws is not None:
            await ws.close()
        if not serve_task.done():
            uv_server.force_exit = uv_server.should_exit = True
            await asyncio.wait_for(serve_task, 20)
        await application.player.shutdown()
        await application.bot.shutdown()


async def wait_for_condition(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "koşul sağlanmadı"
        await asyncio.sleep(0.02)


def test_doctor_reports_a_busy_port_and_fails(settings, monkeypatch, capsys):
    import socket

    import bot
    import main

    monkeypatch.setattr(bot, "find_chrome", lambda configured="": "C:/Chrome/chrome.exe")
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        settings.host, settings.port = "127.0.0.1", busy.getsockname()[1]
        code = main.run_doctor(settings)
    out = capsys.readouterr().out
    for label in ("Chrome / Edge", "yt-dlp", "yt-dlp-ejs", "İndirme klasörü", "Port"):
        assert label in out
    port_line = next(line for line in out.splitlines() if "Port" in line)
    assert "❌" in port_line and "--port" in port_line
    assert code == 1
