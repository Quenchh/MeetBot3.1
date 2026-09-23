"""Bot ekranı: panelden botun tarayıcısını görme / kullanma (Google girişi).

• Sunucu: yetki (yalnızca yönetici + MEETBOT_REMOTE_VIEW), kareler yalnızca izleyiciye,
  girdi doğrulama, izleyici ayrılınca bırakma, hata → view_error / view_closed.
• Bot: gerçek MeetBot'un view_* metotları, sahte bir Google giriş sayfasına karşı
  (Playwright Chromium; _ensure_context dikişi).
• Linux: sanal ekran (Xvfb) gereksinimi ve Chrome bayrakları.
"""

from __future__ import annotations

import base64
import time
import uuid

import pytest
from fastapi.testclient import TestClient

import bot
import server
from bot import (
    GOOGLE_COOKIE_DOMAIN_RE,
    VIEW_KEYS,
    BotError,
    MeetBot,
    build_chrome_args,
    needs_virtual_display,
)
from fakes import FakeDownloader, ScriptedBot
from player import Player
from server import VIEW_TYPES, Hub, create_app, permissions_for

PASSWORD = "test-secret"
LOOPBACK = ("127.0.0.1", 50000)
REMOTE = ("203.0.113.7", 50000)


# ──────────────────────────────────────────────────────────────
#  Saf yardımcılar
# ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("domain,match", [
    (".google.com", True), ("accounts.google.com", True), (".google.com.tr", True), ("google.de", True),
    (".youtube.com", True), ("accounts.youtube.com", True),
    ("evilgoogle.com", False), (".google.com.evil.net", False), (".example.com", False), ("gstatic.com", False),
])
def test_sign_out_cookie_domains(domain, match):
    assert bool(GOOGLE_COOKIE_DOMAIN_RE.search(domain)) is match


def test_view_types_need_admin_and_remote_view():
    assert not set(VIEW_TYPES) & set(permissions_for(False, True, True))
    assert not set(VIEW_TYPES) & set(permissions_for(True, True, False))
    assert set(VIEW_TYPES) <= set(permissions_for(True, False, True))


def test_needs_virtual_display(monkeypatch):
    monkeypatch.setattr(bot.platform, "system", lambda: "Linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert needs_virtual_display() is True
    monkeypatch.setenv("DISPLAY", ":0")
    assert needs_virtual_display() is False
    monkeypatch.setattr(bot.platform, "system", lambda: "Windows")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert needs_virtual_display() is False


def test_linux_chrome_flags(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "_running_as_root_on_linux", lambda: False)
    monkeypatch.setattr(bot.platform, "system", lambda: "Linux")
    args = build_chrome_args("google-chrome", settings, tmp_path / "s.wav", virtual_display=True)
    assert "--disable-dev-shm-usage" in args and "--password-store=basic" in args
    assert "--window-size=1920,1080" in args and "--window-position=0,0" in args
    assert args[-1] == "about:blank"
    monkeypatch.setattr(bot.platform, "system", lambda: "Windows")
    args = build_chrome_args("chrome.exe", settings, tmp_path / "s.wav")
    assert "--password-store=basic" not in args and not any(a.startswith("--window-size") for a in args)


async def test_virtual_display_requires_xvfb(monkeypatch):
    monkeypatch.setattr(bot.shutil, "which", lambda name: None)
    with pytest.raises(BotError, match="Xvfb"):
        await bot.VirtualDisplay().start()


# ──────────────────────────────────────────────────────────────
#  Sunucu protokolü (FakeBot'un sahte bot ekranıyla)
# ──────────────────────────────────────────────────────────────

class Session:
    def __init__(self, ws):
        self.ws = ws
        self.seen: list[dict] = []

    def recv(self) -> dict:
        message = self.ws.receive_json()
        self.seen.append(message)
        return message

    def until(self, predicate, limit: int = 300) -> dict:
        for _ in range(limit):
            message = self.recv()
            if predicate(message):
                return message
        raise AssertionError("beklenen mesaj gelmedi")

    def request(self, kind: str, **fields) -> dict:
        rid = uuid.uuid4().hex[:8]
        self.ws.send_json({"type": kind, "rid": rid, **fields})
        return self.until(lambda m: m.get("type") == "ack" and m.get("rid") == rid)

    def hello(self, name: str = "Vedat") -> dict:
        self.ws.send_json({"type": "hello", "name": name})
        return self.until(lambda m: m.get("type") == "welcome")

    def admin(self) -> None:
        assert self.request("auth", password=PASSWORD)["ok"]

    def count_between_ping(self, kind: str) -> int:
        """Bir ping gönderip pong'a kadar gelen `kind` mesajlarını sayar."""
        start = len(self.seen)
        self.request("ping")
        return sum(1 for m in self.seen[start:] if m.get("type") == kind)


def build(settings):
    fake = ScriptedBot(settings, connected=True)
    hub = Hub()
    player = Player(settings, fake, FakeDownloader(settings.downloads_dir), hub.broadcast)
    return fake, create_app(settings, player, fake, hub)


@pytest.fixture
def fast_view(monkeypatch):
    monkeypatch.setattr(server, "VIEW_INTERVAL", 0.05)
    monkeypatch.setattr(server, "VIEW_ERROR_RETRY", 0.01)


def test_guest_and_remote_admin_cannot_open_the_view(settings):
    fake, app = build(settings)
    with TestClient(app, client=REMOTE) as test_client, test_client.websocket_connect("/ws") as ws:
        session = Session(ws)
        welcome = session.hello()
        assert not set(VIEW_TYPES) & set(welcome["permissions"])
        assert "yönetici" in session.request("view_start", target="login")["message"]
        session.admin()
        perms = next(m for m in reversed(session.seen) if m.get("type") == "session")["permissions"]
        assert not set(VIEW_TYPES) & set(perms)
        ack = session.request("view_start", target="login")
        assert not ack["ok"] and "SSH" in ack["message"]
    assert fake.view_events == []


def test_remote_view_on_allows_any_admin_and_off_blocks_everyone(settings):
    settings.remote_view = "on"
    fake, app = build(settings)
    with TestClient(app, client=REMOTE) as test_client, test_client.websocket_connect("/ws") as ws:
        session = Session(ws)
        session.hello()
        session.admin()
        assert session.request("view_start", target="meet")["ok"]
    settings.remote_view = "off"
    fake, app = build(settings)
    with TestClient(app, client=LOOPBACK) as test_client, test_client.websocket_connect("/ws") as ws:
        session = Session(ws)
        assert session.hello()["limits"]["remote_view"] == "off"
        session.admin()
        ack = session.request("view_start", target="meet")
        assert not ack["ok"] and "kapalı" in ack["message"]


def test_frames_go_only_to_viewers_and_inputs_reach_the_bot(settings, fast_view):
    fake, app = build(settings)
    with TestClient(app, client=LOOPBACK) as test_client, \
            test_client.websocket_connect("/ws") as ws_admin, test_client.websocket_connect("/ws") as ws_other:
        admin, other = Session(ws_admin), Session(ws_other)
        admin.hello("Vedat")
        other.hello("Ayşe")
        admin.admin()
        assert admin.request("view_input", action="click", x=0.5, y=0.5)["message"] == "Önce bot ekranını açın"

        assert admin.request("view_start", target="login")["ok"]
        frame = admin.until(lambda m: m.get("type") == "view_frame")
        assert frame["target"] == "login" and frame["interactive"] is True
        assert frame["image"].startswith("data:image/png;base64,")
        assert frame["url"].startswith("https://accounts.google.com") and frame["signed_in"] is False
        assert base64.b64decode(frame["image"].split(",", 1)[1]).startswith(b"\x89PNG")

        for payload in ({"action": "click", "x": 0.25, "y": 0.75}, {"action": "type", "text": "şifre 123"},
                        {"action": "key", "key": "Enter"}, {"action": "scroll", "dy": 400},
                        {"action": "back"}, {"action": "reload"}):
            assert admin.request("view_input", **payload)["ok"], payload
        assert fake.view_events == [
            ("open", "login"), ("click", 0.25, 0.75), ("type", "şifre 123"), ("key", "Enter"),
            ("scroll", 400.0), ("back",), ("reload",),
        ]
        # Diğer (izlemeyen) oturum hiç kare almadı
        other.request("ping")
        assert not any(m.get("type") == "view_frame" for m in other.seen)

        for bad, fragment in (
            ({"action": "click", "x": 1.5, "y": 0.5}, "0–1"),
            ({"action": "click", "x": "a", "y": 0.5}, "sayı"),
            ({"action": "type", "text": ""}, "metin"),
            ({"action": "type", "text": "x" * 501}, "metin"),
            ({"action": "key", "key": "F12"}, "tuş"),
            ({"action": "hack"}, "Geçersiz"),
        ):
            ack = admin.request("view_input", **bad)
            assert not ack["ok"] and fragment in ack["message"], (bad, ack)
        # Adres çubuğuna gitme özelliği yok (hesap ayarlarına gidilemesin)
        assert "Bilinmeyen" in admin.request("view_navigate", url="https://myaccount.google.com/")["message"]
        assert not admin.request("view_start", target="desktop")["ok"]

        # Meet sekmesi yalnızca izlenir
        assert admin.request("view_start", target="meet")["ok"]
        ack = admin.request("view_input", action="click", x=0.5, y=0.5)
        assert not ack["ok"] and "yalnızca izlenebilir" in ack["message"]

        assert admin.request("view_stop")["ok"]
        assert fake.view_events[-1] == ("release",)


def test_login_tab_closes_once_signed_in_and_logout_reopens_it(settings, fast_view):
    fake, app = build(settings)
    with TestClient(app, client=LOOPBACK) as test_client, test_client.websocket_connect("/ws") as ws:
        session = Session(ws)
        session.hello()
        session.admin()
        assert session.request("view_start", target="login")["ok"]
        session.until(lambda m: m.get("type") == "view_frame" and m["target"] == "login")

        fake.signed_in = True   # kullanıcı giriş sekmesinde oturum açtı
        frame = session.until(lambda m: m.get("type") == "view_frame" and m["signed_in"] is True)
        assert frame["target"] == "meet" and frame["interactive"] is False
        assert ("login_closed",) in fake.view_events
        ack = session.request("view_input", action="type", text="profil ayarı")
        assert not ack["ok"]
        ack = session.request("view_start", target="login")
        assert not ack["ok"] and "zaten bağlı" in ack["message"]

        # Toplantıdayken hesap çıkarılamaz
        ack = session.request("google_logout")
        assert not ack["ok"] and "toplantıdan" in ack["message"]
        fake.status = "disconnected"
        ack = session.request("google_logout")
        assert ack["ok"] and "çıkış" in ack["message"]
        assert fake.signed_in is False
        assert session.request("view_start", target="login")["ok"]


def test_google_logout_needs_admin_and_view_permission(settings):
    fake, app = build(settings)
    with TestClient(app, client=REMOTE) as test_client, test_client.websocket_connect("/ws") as ws:
        session = Session(ws)
        session.hello()
        assert "yönetici" in session.request("google_logout")["message"]
        session.admin()
        assert "SSH" in session.request("google_logout")["message"]
    assert ("sign_out",) not in fake.view_events


def test_unchanged_frames_are_not_resent(settings, fast_view):
    fake, app = build(settings)
    with TestClient(app, client=LOOPBACK) as test_client, test_client.websocket_connect("/ws") as ws:
        session = Session(ws)
        session.hello()
        session.admin()
        session.request("view_start", target="meet")
        session.until(lambda m: m.get("type") == "view_frame")
        time.sleep(0.3)   # ~6 döngü: kare değişmedi
        assert session.count_between_ping("view_frame") == 0
        fake.signed_in = True   # oturum açıldı → yeni kare
        frame = session.until(lambda m: m.get("type") == "view_frame")
        assert frame["signed_in"] is True


def test_disconnect_and_logout_release_the_view(settings, fast_view):
    fake, app = build(settings)
    with TestClient(app, client=LOOPBACK) as test_client:
        with test_client.websocket_connect("/ws") as ws:
            session = Session(ws)
            session.hello()
            session.admin()
            session.request("view_start", target="login")
        deadline = time.monotonic() + 3
        while fake.view_events[-1] != ("release",) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert fake.view_events[-1] == ("release",)

        with test_client.websocket_connect("/ws") as ws:
            session = Session(ws)
            session.hello()
            session.admin()
            session.request("view_start", target="meet")
            session.request("logout")
            assert fake.view_events[-1] == ("release",)
            ack = session.request("view_input", action="key", key="Enter")
            assert not ack["ok"] and "yönetici" in ack["message"]


def test_frame_errors_are_reported_then_the_view_closes(settings, fast_view):
    fake, app = build(settings)

    async def broken_frame():
        raise BotError("Bot ekranı alınamadı")

    fake.view_frame = broken_frame
    with TestClient(app, client=LOOPBACK) as test_client, test_client.websocket_connect("/ws") as ws:
        session = Session(ws)
        session.hello()
        session.admin()
        session.request("view_start", target="meet")
        error = session.until(lambda m: m.get("type") == "view_error")
        assert error["message"] == "Bot ekranı alınamadı"
        closed = session.until(lambda m: m.get("type") == "view_closed")
        assert "alınamadı" in closed["message"]
        assert sum(1 for m in session.seen if m.get("type") == "view_error") == 1
        ack = session.request("view_input", action="key", key="Enter")
        assert ack["message"] == "Önce bot ekranını açın"


# ──────────────────────────────────────────────────────────────
#  Gerçek MeetBot: bot ekranı (Chromium + sahte Google giriş sayfası)
# ──────────────────────────────────────────────────────────────

LOGIN_HTML = """<!doctype html><html><head><title>Sahte Google Girişi</title></head>
<body style="margin:0;font:20px sans-serif">
<form id="f" style="position:absolute;left:100px;top:100px" onsubmit="event.preventDefault();document.title='gönderildi:'+document.getElementById('email').value">
<input id="email" style="width:400px;height:40px;font-size:20px" autocomplete="off">
<button id="next">İleri</button></form>
<div style="height:3000px"></div></body></html>"""


class ViewBot(MeetBot):
    def __init__(self, settings, context):
        super().__init__(settings)
        self.test_context = context

    async def _ensure_context(self, announce=True):
        self.announced = announce
        return self.test_context


@pytest.fixture
async def view_bot(chromium, settings):
    context = await chromium.new_context(viewport={"width": 1000, "height": 700})

    async def serve(route):
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=LOGIN_HTML)

    await context.route("https://accounts.google.com/**", serve)
    await context.route("https://myaccount.google.com/**", serve)
    meet_bot = ViewBot(settings, context)
    yield meet_bot
    await meet_bot.shutdown()
    await context.close()


@pytest.mark.browser
async def test_login_tab_frames_clicks_and_typing(view_bot):
    await view_bot.view_open("login")
    assert view_bot.announced is False   # tarayıcıyı açmak "bağlanıyor" durumu yaymaz
    frame = await view_bot.view_frame()
    assert frame["target"] == "login" and frame["interactive"] is True
    assert frame["title"] == "Sahte Google Girişi"
    assert frame["url"].startswith("https://accounts.google.com")
    assert (frame["width"], frame["height"]) == (1000, 700)
    assert base64.b64decode(frame["image"].split(",", 1)[1])[:3] == b"\xff\xd8\xff"   # JPEG
    assert frame["signed_in"] is False

    login = view_bot._login_page
    box = await login.locator("#email").bounding_box()
    await view_bot.view_click((box["x"] + 20) / 1000, (box["y"] + 10) / 700)
    await view_bot.view_type("bot@örnek.com")
    await view_bot.view_key("Enter")
    await login.wait_for_function("document.title.startsWith('gönderildi')")
    assert await login.title() == "gönderildi:bot@örnek.com"

    await view_bot.view_scroll(500)
    await login.wait_for_function("window.scrollY > 0")
    with pytest.raises(BotError):
        await view_bot.view_key("F12")
    with pytest.raises(BotError):
        await view_bot.view_click(1.2, 0.5)
    assert not hasattr(view_bot, "view_navigate")   # adres çubuğu yok

    # Aynı giriş sekmesi yeniden kullanılır; Meet sekmesi yalnızca izlenir
    await view_bot.view_open("login")
    assert view_bot._login_page is login
    meet_tab = await view_bot.test_context.new_page()
    await view_bot.view_open("meet")
    assert login.is_closed() and view_bot._login_page is None
    frame = await view_bot.view_frame()
    assert frame["target"] == "meet" and frame["interactive"] is False and frame["url"] == meet_tab.url
    with pytest.raises(BotError, match="yalnızca izlenebilir"):
        await view_bot.view_click(0.5, 0.5)
    await view_bot.view_release()


@pytest.mark.browser
async def test_login_tab_closes_the_moment_google_signs_in(view_bot):
    await view_bot.test_context.new_page()   # botun kendi sekmesi
    await view_bot.view_open("login")
    login = view_bot._login_page
    # Google oturumu açıldı (çerez yazıldı): bir sonraki girdi hesap sayfasına ULAŞMAZ, sekme kapanır
    await view_bot.test_context.add_cookies([{"name": "__Secure-1PSID", "value": "x", "domain": ".google.com",
                                             "path": "/", "secure": True}])
    with pytest.raises(BotError, match="bağlandı"):
        await view_bot.view_type("profil ayarı")
    assert login.is_closed() and view_bot._login_page is None
    frame = await view_bot.view_frame()
    assert frame["target"] == "meet" and frame["signed_in"] is True and frame["interactive"] is False
    with pytest.raises(BotError, match="zaten bağlı"):
        await view_bot.view_open("login")

    # Hesabı çıkar: yalnızca Google/YouTube çerezleri silinir, giriş sekmesi yeniden açılabilir
    await view_bot.test_context.add_cookies([{"name": "keep", "value": "1", "domain": "example.com", "path": "/"}])
    await view_bot.google_sign_out()
    names = {c["name"] for c in await view_bot.test_context.cookies()}
    assert "__Secure-1PSID" not in names and "keep" in names
    assert await view_bot.google_signed_in(fresh=True) is False
    await view_bot.view_open("login")
    assert view_bot._login_page is not None and view_bot._login_page is not login


@pytest.mark.browser
async def test_frame_closes_the_login_tab_after_sign_in(view_bot):
    await view_bot.test_context.new_page()
    await view_bot.view_open("login")
    await view_bot.test_context.add_cookies([{"name": "SID", "value": "x", "domain": ".google.com", "path": "/",
                                             "secure": True}])
    frame = await view_bot.view_frame()   # kare alınırken fark edilir, kapatılır
    assert frame["target"] == "meet" and view_bot._login_page is None


async def test_sign_out_is_refused_during_a_meeting(settings):
    meet_bot = MeetBot(settings)
    meet_bot._status = "connected"
    with pytest.raises(BotError, match="toplantıdan"):
        await meet_bot.google_sign_out()


@pytest.mark.browser
async def test_last_tab_is_blanked_instead_of_closed(view_bot):
    # Giriş sekmesi tarayıcıdaki SON sekmeyse kapatılmaz (Chrome da kapanırdı): boşaltılır
    await view_bot.view_open("login")
    login = view_bot._login_page
    await view_bot.view_open("meet")
    assert not login.is_closed() and login.url == "about:blank" and view_bot._login_page is None
    assert (await view_bot.view_frame())["target"] == "meet"


@pytest.mark.browser
async def test_view_without_browser_reports_error(settings):
    meet_bot = MeetBot(settings)
    with pytest.raises(BotError, match="açık değil"):
        await meet_bot.view_frame()
    with pytest.raises(BotError):
        await meet_bot.view_open("desktop")


def test_view_keys_match_the_frontend():
    app_js = (bot.BASE_DIR / "static" / "app.js").read_text(encoding="utf-8")
    start = app_js.index("const VIEW_KEYS = new Set([")
    listed = app_js[start:app_js.index("]);", start)]
    for key in VIEW_KEYS - {"Shift+Tab", "Control+a", "Space"}:
        assert f'"{key}"' in listed, key


async def test_shutdown_still_stops_chrome_and_xvfb_when_the_driver_died(settings):
    # systemd tüm gruba SIGTERM gönderirse Playwright sürücüsü botdan önce ölür: sonraki adımlar yine çalışmalı
    meet_bot = MeetBot(settings)
    calls = []

    async def dead_driver():
        calls.append("close_browser")
        raise Exception("Browser.new_browser_cdp_session: Connection closed while reading from the driver")

    async def stop_chrome():
        calls.append("stop_chrome")

    async def stop_display():
        calls.append("stop_display")

    meet_bot._close_browser = dead_driver
    meet_bot._stop_chrome = stop_chrome
    meet_bot._display.stop = stop_display
    await meet_bot.shutdown()
    assert calls == ["close_browser", "stop_chrome", "stop_display"]
