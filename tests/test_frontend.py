"""MeetBot 3.2 web arayüzü testleri (static/).

İki grup test var:
  • Statik denetimler: tarayıcı gerekmez (derlenmiş Tailwind, ikon alt kümesi, innerHTML yasağı, sürüm etiketi).
  • Tarayıcı testleri: Playwright'ın paketli Chromium'u gerçek index.html + app.js dosyalarını yükler.
    /ws bağlantısı page.route_web_socket ile protokol v2 konuşan sahte bir sunucuya yönlendirilir.
    Ağ gerekmez: statik dosyalar diskten okunur, dış kaynaklar (font, küçük resim) boş yanıtla karşılanır.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
BASE = "http://meetbot.test"
WS_URL = "ws://meetbot.test/ws"
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

GUEST_PERMISSIONS = ["hello", "auth", "logout", "add", "remove", "ping"]
CONTROL_PERMISSIONS = ["move", "play_now", "shuffle", "pause", "resume", "skip", "seek", "repeat", "volume"]
ADMIN_ONLY_PERMISSIONS = ["clear", "stop", "mic", "join_meet", "leave_meet"]
ADMIN_PERMISSIONS = GUEST_PERMISSIONS + CONTROL_PERMISSIONS + ADMIN_ONLY_PERMISSIONS
MEET_LINK = "https://meet.google.com/abc-defg-hij"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


# ──────────────────────────────────────────────────────────────
#  Statik denetimler
# ──────────────────────────────────────────────────────────────

def test_index_uses_compiled_tailwind_instead_of_play_cdn():
    html = read("index.html")
    assert "cdn.tailwindcss.com" not in html
    assert 'type="text/tailwindcss"' not in html
    assert "font-awesome" not in html.lower()
    assert '/static/tailwind.css?v=3.2.0' in html
    assert (STATIC / "tailwind.css").is_file()
    assert (STATIC / "src" / "input.css").is_file()
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert "tailwindcss@3" in package["scripts"]["build:css"]
    assert "static/tailwind.css" in package["scripts"]["build:css"]


def test_version_label_is_3_2_everywhere():
    html = read("index.html")
    app = read("app.js")
    assert "<title>MeetBot 3.2" in html
    assert ">MeetBot 3.2</h1>" in html                      # giriş ekranı
    assert 'text-teal opacity-80">3.2</span>' in html        # başlık
    assert 'id="app-version">v3.2<' in html                 # alt bilgi
    for asset in ("tailwind.css", "style.css", "app.js"):
        assert f"/static/{asset}?v=3.2.0" in html
    assert "MeetBot 3.2" in app
    for stale in ("MeetBot 2.0", "v.2.0.84", "?v=6.0", "password123"):
        assert stale not in html and stale not in app
    assert json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"] == "3.2.0"


def test_app_js_has_no_html_injection_sinks():
    app = read("app.js")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in app, f"app.js içinde yasaklı HTML/JS enjeksiyon noktası: {sink}"


CUSTOM_COLOR_CLASS = re.compile(
    r"(?<![\w-])((?:[a-z]+:)*(?:text|bg|border|from|to|via|shadow|ring|accent|outline|decoration|placeholder)"
    r"-(?:teal|fuchsia|sunset|purple)(?:/\d+)?)(?![\w-])"
)


def css_escape(class_name: str) -> str:
    return re.sub(r"([:/.\[\]])", r"\\\1", class_name)


def test_custom_theme_color_classes_are_compiled():
    css = read("tailwind.css")
    assert "--teal-rgb:0 242 255" in css.replace(": ", ":")
    used = set(CUSTOM_COLOR_CLASS.findall(read("index.html"))) | set(CUSTOM_COLOR_CLASS.findall(read("app.js")))
    assert {"text-teal", "bg-sunset", "border-fuchsia", "bg-teal"} <= used
    missing = sorted(name for name in used if f".{css_escape(name)}" not in css)
    assert not missing, f"static/tailwind.css eski, 'npm run build:css' çalıştırın. Eksik sınıflar: {missing}"


def icon_subset() -> list[str]:
    match = re.search(r"icon_names=([a-z_,]+)", read("index.html"))
    assert match, "Material Symbols bağlantısında icon_names parametresi yok"
    return match.group(1).split(",")


def test_icon_font_request_is_sorted_and_unique():
    names = icon_subset()
    assert names == sorted(names), "Google Fonts icon_names listesi alfabetik olmalı"
    assert len(names) == len(set(names))


# ──────────────────────────────────────────────────────────────
#  Sahte veri
# ──────────────────────────────────────────────────────────────

def fmt(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


def track(track_id: int, title: str | None = None, *, duration: int | None = 200, added_by: str = "Ayşe",
          status: str = "ready", thumbnail: str | None = None) -> dict:
    video_id = f"vid{track_id:08d}"
    return {
        "id": track_id,
        "video_id": video_id,
        "title": title if title is not None else f"Şarkı {track_id}",
        "duration": duration,
        "duration_str": fmt(duration),
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail": thumbnail if thumbnail is not None else f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
        "added_by": added_by,
        "added_at": "2026-09-23T10:15:00Z",
        "status": status,
    }


def snapshot(**overrides) -> dict:
    snap = {
        "queue": [],
        "current": None,
        "playback": {"state": "idle", "position": 0, "duration": 0, "repeat": "off"},
        "volume": {"music": 80, "mic": 80},
        "mic_muted": False,
        "bot": {"status": "disconnected", "meet_link": None, "detail": None},
        "history": [],
        "listeners": ["Vedat"],
    }
    snap.update(overrides)
    return snap


def welcome(name: str = "Vedat", *, admin: bool = False, permissions: list[str] | None = None,
            state: dict | None = None, guest_controls: bool = True) -> dict:
    if permissions is None:
        permissions = ADMIN_PERMISSIONS if admin else GUEST_PERMISSIONS + (CONTROL_PERMISSIONS if guest_controls else [])
    return {
        "type": "welcome",
        "client_id": "c-1",
        "name": name,
        "is_admin": admin,
        "permissions": permissions,
        "version": "3.2.0",
        "limits": {"max_queue": 100, "max_user_queue": 0, "max_duration": 1200, "playlist_limit": 25,
                   "guest_controls": guest_controls},
        "state": state or snapshot(),
    }


def playing_state(**overrides) -> dict:
    values = {
        "queue": [track(1, added_by="Vedat"), track(2), track(3, added_by="Mehmet")],
        "current": track(9, "Çalan Şarkı", added_by="Zeynep"),
        "playback": {"state": "playing", "position": 10, "duration": 200, "repeat": "off"},
        "bot": {"status": "connected", "meet_link": MEET_LINK, "detail": "Toplantıya katıldı"},
    }
    values.update(overrides)
    return snapshot(**values)


# ──────────────────────────────────────────────────────────────
#  Sahte sunucu + tarayıcı düzeneği
# ──────────────────────────────────────────────────────────────

Responder = Callable[["FakeServer", object, dict], "dict | None"]


def broadcast_volume(server: "FakeServer", ws, msg: dict) -> dict:
    """Gerçek sunucu gibi: önce herkese "volume" yayını, sonra ack (player.set_volume sırası)."""
    server.volume[msg["target"]] = msg["value"]
    ws.send(json.dumps({"type": "volume", **server.volume}))
    return {"ok": True}


class FakeServer:
    """Protokol v2 konuşan sahte sunucu. Varsayılan: hello → welcome, rid'li her mesaj → ok ack."""

    def __init__(self, welcome_msg: dict):
        self.welcome = welcome_msg
        self.connections: list = []
        self.received: list[dict] = []
        self.responders: dict[str, Responder | None] = {"volume": broadcast_volume}
        self.volume = dict(welcome_msg["state"]["volume"])
        self.refuse_connections = False
        self.answer_pings = True
        self.reject_hello: str | None = None
        self.hold_hello = False          # True → hello yanıtsız bekletilir (release_hello ile yanıtlanır)
        self.held_hellos: list = []
        self.pending_refusals: list = []

    # Playwright bu fonksiyonu her yeni WebSocket için çağırır
    def handle(self, ws) -> None:
        self.connections.append(ws)
        if self.refuse_connections:
            # İşleyici içinden ws.close() çağırmak sync API'de kilitlenir; test akışından kapatılır
            self.pending_refusals.append(ws)
            return
        ws.on_message(lambda raw: self._on_message(ws, raw))

    def close_refused(self) -> None:
        while self.pending_refusals:
            self.pending_refusals.pop(0).close(code=1011, reason="kapalı")

    def _on_message(self, ws, raw: str) -> None:
        msg = json.loads(raw)
        self.received.append(msg)
        kind, rid = msg.get("type"), msg.get("rid")
        if kind == "hello":
            if self.hold_hello:
                self.held_hellos.append((ws, rid))
            else:
                self._answer_hello(ws, rid)
            return
        if kind == "ping":
            if self.answer_pings:
                ws.send(json.dumps({"type": "pong"}))
            return
        if kind in self.responders:
            responder = self.responders[kind]
            result = responder(self, ws, msg) if responder else None
        else:
            result = {"ok": True}
        if result is not None and rid:
            self.ack(rid, ws=ws, **result)

    def _answer_hello(self, ws, rid: str | None) -> None:
        if self.reject_hello:
            ws.send(json.dumps({"type": "ack", "rid": rid, "ok": False, "message": self.reject_hello, "data": None}))
            return
        ws.send(json.dumps(self.welcome))
        if rid:
            ws.send(json.dumps({"type": "ack", "rid": rid, "ok": True, "message": None, "data": None}))

    def release_hello(self) -> None:
        while self.held_hellos:
            self._answer_hello(*self.held_hellos.pop(0))

    def ack(self, rid: str, *, ok: bool = True, message: str | None = None, data=None, ws=None) -> None:
        (ws or self.connections[-1]).send(json.dumps({"type": "ack", "rid": rid, "ok": ok, "message": message, "data": data}))

    def push(self, msg: dict | str) -> None:
        self.connections[-1].send(msg if isinstance(msg, str) else json.dumps(msg))

    def close(self, code: int = 1012) -> None:
        self.connections[-1].close(code=code)

    def sent(self, kind: str) -> list[dict]:
        return [msg for msg in self.received if msg.get("type") == kind]


SEED_STORAGE_JS = """
(() => {
    if (sessionStorage.getItem("__seeded")) return;
    sessionStorage.setItem("__seeded", "1");
    const values = %s;
    for (const [key, value] of Object.entries(values)) localStorage.setItem(key, value);
})();
"""

# Oturum boyunca ekrana basılan tüm Material Symbols adlarını toplar (ikon alt kümesi testi için)
ICON_COLLECTOR_JS = """
(() => {
    window.__icons = new Set();
    const add = (node) => {
        if (node.nodeType !== 1) return;
        if (node.classList.contains("material-symbols-outlined")) window.__icons.add(node.textContent.trim());
        node.querySelectorAll(".material-symbols-outlined").forEach((n) => window.__icons.add(n.textContent.trim()));
    };
    new MutationObserver((mutations) => {
        for (const m of mutations) {
            add(m.target.nodeType === 1 ? m.target : m.target.parentElement || document.createElement("i"));
            m.addedNodes.forEach(add);
        }
    }).observe(document, { subtree: true, childList: true, characterData: true });
    document.addEventListener("DOMContentLoaded", () => add(document.documentElement));
})();
"""


def serve_static(route) -> None:
    path = urlsplit(route.request.url).path
    if path == "/":
        target = STATIC / "index.html"
    elif path.startswith("/static/"):
        target = (STATIC / path[len("/static/"):]).resolve()
    else:
        target = None
    if target is not None and target.is_file() and STATIC.resolve() in target.resolve().parents:
        route.fulfill(path=str(target))
    else:
        route.fulfill(status=404, body="")


@dataclass
class App:
    page: object
    server: FakeServer
    errors: list[str]
    dialogs: list[str] = field(default_factory=list)
    statue_requests: list[str] = field(default_factory=list)

    def wait_until(self, predicate: Callable[[], bool], what: str, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError(f"Zaman aşımı: {what}")
            self.page.wait_for_timeout(20)
            self.server.close_refused()

    def wait_sent(self, kind: str, count: int = 1) -> dict:
        self.wait_until(lambda: len(self.server.sent(kind)) >= count, f"{count}. '{kind}' mesajı")
        return self.server.sent(kind)[count - 1]

    def wait_connections(self, count: int) -> None:
        self.wait_until(lambda: len(self.server.connections) >= count, f"{count}. WebSocket bağlantısı")

    def wait_ready(self) -> None:
        expect(self.page.locator("#admin-toggle-btn")).to_be_enabled()

    def pause_clock(self) -> None:
        # Sayı verilirse Playwright bunu saniye kabul eder; sayfa saatinden 10 ms sonrasına datetime ile dur
        now_ms = self.page.evaluate("Date.now()")
        self.page.clock.pause_at(datetime.fromtimestamp((now_ms + 10) / 1000, tz=timezone.utc))

    def tick(self, ms: int) -> None:
        self.page.clock.run_for(ms)
        self.page.wait_for_timeout(30)   # yeni bağlantı / mesaj olaylarının işlenmesi için
        self.server.close_refused()
        self.page.wait_for_timeout(30)

    def toasts(self) -> list[str]:
        return self.page.locator("#toast-container .toast-text").all_inner_texts()


try:
    from playwright.sync_api import expect
except ImportError:  # pragma: no cover - playwright requirements.txt içinde
    expect = None


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        playwright = sync_api.sync_playwright().start()
    except Exception as exc:  # pragma: no cover - ortam bağımlı
        pytest.skip(f"Playwright başlatılamadı: {exc}")
    try:
        chromium = playwright.chromium.launch()
    except Exception as exc:  # pragma: no cover - ortam bağımlı
        playwright.stop()
        pytest.skip(f"Playwright Chromium bulunamadı (playwright install chromium): {exc}")
    yield chromium
    chromium.close()
    playwright.stop()


@pytest.fixture
def open_app(browser):
    contexts = []

    def _open(*, name: str | None = "Vedat", token: str | None = None, welcome_msg: dict | None = None,
              viewport: tuple[int, int] = (1280, 900), clock: bool = False, statue_status: int = 200,
              reject_hello: str | None = None, hold_hello: bool = False, touch: bool = False) -> App:
        # touch=True → dokunmatik telefon (has_touch + is_mobile): CDP ile gerçek dokunma olayları gönderilebilir
        context = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]},
                                      has_touch=touch, is_mobile=touch)
        contexts.append(context)
        page = context.new_page()
        page.set_default_timeout(5000)
        server = FakeServer(welcome_msg or welcome(name or "Vedat"))
        server.reject_hello = reject_hello
        server.hold_hello = hold_hello
        app = App(page, server, [])

        page.on("console", lambda m: app.errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda exc: app.errors.append(f"pageerror: {exc}"))

        def on_dialog(dialog):
            app.dialogs.append(dialog.message)
            dialog.accept()

        page.on("dialog", on_dialog)
        seed = {"meetbot_username": name} if name else {}
        if token:
            seed["meetbot_admin_token"] = token
        page.add_init_script(script=SEED_STORAGE_JS % json.dumps(seed))
        page.add_init_script(script=ICON_COLLECTOR_JS)
        page.add_init_script(script="Math.random = () => 0.5;")   # yeniden bağlanma sapmasını sabitle
        page.route(f"{BASE}/**", serve_static)
        page.route("https://i.ytimg.com/**", lambda r: r.fulfill(status=200, body=PNG_1PX, content_type="image/png"))
        statue_body = PNG_1PX if statue_status == 200 else b""

        def serve_statue(route) -> None:
            app.statue_requests.append(route.request.url)
            route.fulfill(status=statue_status, body=statue_body, content_type="image/png")

        page.route("https://lh3.googleusercontent.com/**", serve_statue)
        page.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, body="", content_type="text/css"))
        page.route_web_socket(WS_URL, server.handle)
        if clock:
            page.clock.install()
        page.goto(BASE + "/")
        if clock:
            app.pause_clock()
        return app

    yield _open
    for context in contexts:
        context.close()


def assert_no_errors(app: App) -> None:
    assert app.errors == [], "\n".join(app.errors)


# Kaydırıcılardaki input / change / pointercancel olaylarını sırayla kaydeder (düzeneğin gerçekten
# tarayıcı davranışını tetiklediğini doğrulamak için; app.js dinleyicilerinden SONRA çalışır)
RANGE_EVENT_LOG_JS = """() => {
    window.__rangeEvents = [];
    for (const id of ["np-seek", "music-volume-slider", "mic-volume-slider"]) {
        for (const type of ["input", "change", "pointercancel"]) {
            document.getElementById(id).addEventListener(type, () => window.__rangeEvents.push(`${id}:${type}`));
        }
    }
}"""


def touch_gesture(page, selector: str, frac: float, moves: list[tuple[float, float]]) -> None:
    """Gerçek parmak hareketi (CDP Input.dispatchTouchEvent): öğenin genişliğinin `frac` noktasına
    dokun, her adımda (dx, dy) kadar kaydır, bırak. Tarayıcı kaydırmayı/kaydırıcıyı kendisi işler."""
    box = page.locator(selector).bounding_box()
    x, y = box["x"] + box["width"] * frac, box["y"] + box["height"] / 2
    cdp = page.context.new_cdp_session(page)
    try:
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        for dx, dy in moves:
            x, y = x + dx, y + dy
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y}]})
            page.wait_for_timeout(16)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    finally:
        cdp.detach()


# ──────────────────────────────────────────────────────────────
#  Giriş ve el sıkışma (hello / welcome)
# ──────────────────────────────────────────────────────────────

@pytest.mark.browser
def test_first_visit_asks_for_name_then_sends_hello_first(open_app):
    app = open_app(name=None)
    page = app.page
    expect(page.locator("#login-overlay")).to_be_visible()
    expect(page.locator("#app")).to_be_hidden()
    assert app.server.connections == [], "isim girilmeden bağlanılmamalı"

    page.click("#login-btn")
    expect(page.locator("#login-error")).to_have_text("Kullanıcı adı boş olamaz!")

    page.fill("#login-username", "  Vedat  ")
    page.press("#login-username", "Enter")
    hello = app.wait_sent("hello")
    assert app.server.received[0] is hello, "ilk mesaj hello olmalı"
    assert hello["name"] == "Vedat" and "token" not in hello and hello["rid"]
    app.wait_ready()
    expect(page.locator("#login-overlay")).to_be_hidden()
    expect(page.locator("#user-badge-name")).to_have_text("Vedat")
    assert page.evaluate("localStorage.getItem('meetbot_username')") == "Vedat"
    assert_no_errors(app)


@pytest.mark.browser
def test_rejected_hello_returns_to_login_without_retry_loop(open_app):
    app = open_app(name="Kötü İsim", clock=True, reject_hello="Geçersiz kullanıcı adı")
    page = app.page
    expect(page.locator("#login-overlay")).to_be_visible()
    expect(page.locator("#login-error")).to_have_text("Geçersiz kullanıcı adı")
    app.tick(60_000)
    assert len(app.server.connections) == 1, "reddedilen isimle yeniden bağlanmaya çalışmamalı"
    expect(page.locator("#conn-banner")).to_be_hidden()

    app.server.reject_hello = None
    page.fill("#login-username", "Vedat")
    page.click("#login-btn")
    app.wait_connections(2)
    assert app.wait_sent("hello", 2)["name"] == "Vedat"
    app.wait_ready()


@pytest.mark.browser
def test_user_can_change_name_which_reconnects_with_new_hello(open_app):
    app = open_app()
    app.wait_ready()
    page = app.page
    page.click("#user-badge")
    expect(page.locator("#login-overlay")).to_be_visible()
    expect(page.locator("#login-username")).to_have_value("Vedat")
    page.keyboard.press("Escape")
    expect(page.locator("#login-overlay")).to_be_hidden()

    page.click("#user-badge")
    page.fill("#login-username", "Zeynep")
    page.click("#login-btn")
    assert app.wait_sent("hello", 2)["name"] == "Zeynep"
    assert page.evaluate("localStorage.getItem('meetbot_username')") == "Zeynep"


@pytest.mark.browser
def test_hello_timeout_retries_instead_of_showing_login(open_app):
    app = open_app(clock=True, hold_hello=True)
    page = app.page
    app.wait_sent("hello")
    app.tick(15_000)   # sunucu hello'ya hiç yanıt vermedi: isim reddi değil, bağlantı sorunu
    expect(page.locator("#login-overlay")).to_be_hidden()
    expect(page.locator("#conn-banner-status")).to_have_text("Sunucu bağlantısı koptu")

    app.server.hold_hello = False
    app.tick(1_010)
    app.wait_connections(2)
    app.wait_ready()
    expect(page.locator("#conn-banner")).to_be_hidden()


@pytest.mark.browser
def test_reconnect_does_not_close_an_open_name_dialog(open_app):
    app = open_app(clock=True)
    app.wait_ready()
    page = app.page
    page.click("#user-badge")
    page.fill("#login-username", "Yeni İsim")
    app.server.close()
    app.tick(1_010)
    app.wait_connections(2)
    app.wait_ready()
    expect(page.locator("#login-overlay")).to_be_visible()
    expect(page.locator("#login-username")).to_have_value("Yeni İsim")


# ──────────────────────────────────────────────────────────────
#  Yetkiye göre kontroller
# ──────────────────────────────────────────────────────────────

@pytest.mark.browser
def test_guest_without_controls_only_gets_permitted_actions(open_app):
    app = open_app(welcome_msg=welcome(state=playing_state(), guest_controls=False))
    app.wait_ready()
    page = app.page
    for selector in ("#btn-playpause", "#btn-skip", "#btn-stop", "#btn-repeat", "#np-seek",
                     "#music-volume-slider", "#mic-volume-slider", "#btn-toggle-mic", "#btn-shuffle"):
        expect(page.locator(selector)).to_be_disabled()
    expect(page.locator("#btn-stop")).to_have_attribute("title", "Bu işlem için yönetici yetkisi gerekli")
    expect(page.locator("#btn-clear")).to_be_hidden()
    expect(page.locator("#meet-join-btn")).to_be_hidden()
    expect(page.locator("#meet-leave-btn")).to_be_hidden()
    expect(page.locator("#meet-link-input")).to_have_value(MEET_LINK)
    assert page.locator("#meet-link-input").evaluate("el => el.readOnly")
    expect(page.locator("#add-song-btn")).to_be_enabled()

    items = page.locator("#queue-list > li")
    expect(items).to_have_count(3)
    # Kendi eklediği şarkıyı kaldırabilir, başkasınınkine dokunamaz; kimse sürükleyemez
    assert items.nth(0).locator("button").evaluate_all("els => els.map(e => e.dataset.action)") == ["remove"]
    expect(items.nth(1).locator("button")).to_have_count(0)
    assert items.nth(0).get_attribute("draggable") is None
    assert_no_errors(app)


@pytest.mark.browser
def test_guest_controls_enable_playback_but_not_admin_actions(open_app):
    app = open_app(welcome_msg=welcome(state=playing_state(), guest_controls=True))
    app.wait_ready()
    page = app.page
    for selector in ("#btn-playpause", "#btn-skip", "#btn-repeat", "#np-seek", "#music-volume-slider", "#btn-shuffle"):
        expect(page.locator(selector)).to_be_enabled()
    for selector in ("#btn-stop", "#btn-toggle-mic"):
        expect(page.locator(selector)).to_be_disabled()
    expect(page.locator("#btn-clear")).to_be_hidden()
    second = page.locator("#queue-list > li").nth(1)
    assert second.locator("button").evaluate_all("els => els.map(e => e.dataset.action)") == \
        ["play_now", "up", "down", "remove"]
    assert second.get_attribute("draggable") == "true"


@pytest.mark.browser
def test_remove_button_follows_the_permissions_list(open_app):
    permissions = [name for name in GUEST_PERMISSIONS if name != "remove"]
    app = open_app(welcome_msg=welcome(state=playing_state(), permissions=permissions, guest_controls=False))
    app.wait_ready()
    expect(app.page.locator("#queue-list > li")).to_have_count(3)
    expect(app.page.locator("#queue-list button")).to_have_count(0)   # kendi şarkısı için bile


@pytest.mark.browser
def test_play_controls_explain_when_bot_is_not_in_meeting(open_app):
    state = playing_state(playback={"state": "paused", "position": 30, "duration": 200, "repeat": "off"},
                          bot={"status": "disconnected", "meet_link": None, "detail": "Toplantıdan ayrıldı"})
    app = open_app(welcome_msg=welcome(admin=True, state=state))
    app.wait_ready()
    page = app.page
    play = page.locator("#btn-playpause")
    # Sunucu bot toplantıda değilken resume / play_now'ı reddeder: tıklatıp hata göstermek yerine açıkla
    expect(play).to_be_disabled()
    expect(play).to_have_attribute("title", "Bot toplantıda değil")
    expect(queue_button(page, 0, "play_now")).to_be_disabled()
    expect(queue_button(page, 0, "play_now")).to_have_attribute("title", "Şimdi çal (bot toplantıda değil)")
    expect(page.locator("#np-hint")).to_contain_text("kaldığı yerden devam edecek")
    expect(page.locator("#btn-skip")).to_be_enabled()
    expect(page.locator("#bot-badge")).to_have_attribute("role", "status")

    app.server.push({"type": "bot", "status": "connected", "meet_link": MEET_LINK, "detail": None})
    expect(play).to_be_enabled()
    assert play.get_attribute("title") is None
    queue_button(page, 0, "play_now").click()
    assert app.wait_sent("play_now")["id"] == 1
    play.click()
    app.wait_sent("resume")
    assert_no_errors(app)


# ──────────────────────────────────────────────────────────────
#  Yönetici girişi
# ──────────────────────────────────────────────────────────────

def auth_responder(server: FakeServer, ws, msg: dict) -> dict:
    if msg.get("password") != "test-secret":
        return {"ok": False, "message": "Hatalı şifre"}
    ws.send(json.dumps({"type": "session", "is_admin": True, "permissions": ADMIN_PERMISSIONS}))
    return {"ok": True, "data": {"token": "tok-123"}}


@pytest.mark.browser
def test_admin_login_is_server_validated_and_token_reauths_after_reconnect(open_app):
    app = open_app(welcome_msg=welcome(state=playing_state()), clock=True)
    app.server.responders["auth"] = auth_responder
    app.wait_ready()
    page = app.page

    page.click("#admin-toggle-btn")
    dialog = page.locator("#admin-form")
    expect(dialog).to_be_visible()
    expect(dialog).to_have_attribute("role", "dialog")
    expect(page.locator("#admin-password")).to_be_focused()
    page.fill("#admin-password", "yanlis")
    page.click("#admin-login-btn")
    expect(page.locator("#admin-error")).to_have_text("Hatalı şifre")
    expect(dialog).to_be_visible()
    assert page.evaluate("localStorage.getItem('meetbot_admin_token')") is None

    page.fill("#admin-password", "test-secret")
    page.press("#admin-password", "Enter")
    expect(dialog).to_be_hidden()
    assert app.server.sent("auth")[-1]["password"] == "test-secret"
    assert page.evaluate("localStorage.getItem('meetbot_admin_token')") == "tok-123"
    expect(page.locator("#admin-toggle-text")).to_have_text("Yönetici")
    expect(page.locator("#btn-clear")).to_be_visible()
    expect(page.locator("#meet-leave-btn")).to_be_visible()
    expect(page.locator("#btn-toggle-mic")).to_be_enabled()

    # Sunucu yeniden başladı: yeni bağlantıdaki hello saklanan jetonu taşır
    app.server.welcome = welcome(admin=True, state=playing_state())
    app.server.close()
    expect(page.locator("#conn-banner")).to_be_visible()
    app.tick(1_010)
    app.wait_connections(2)
    assert app.wait_sent("hello", 2)["token"] == "tok-123"
    app.wait_ready()
    expect(page.locator("#admin-toggle-text")).to_have_text("Yönetici")
    assert_no_errors(app)


@pytest.mark.browser
def test_admin_modal_closes_with_escape_and_backdrop(open_app):
    app = open_app()
    app.wait_ready()
    page = app.page
    page.click("#admin-toggle-btn")
    expect(page.locator("#admin-modal-overlay")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#admin-modal-overlay")).to_be_hidden()
    expect(page.locator("#admin-toggle-btn")).to_be_focused()

    page.click("#admin-toggle-btn")
    page.mouse.click(5, 5)
    expect(page.locator("#admin-modal-overlay")).to_be_hidden()


@pytest.mark.browser
def test_expired_admin_token_is_dropped_with_warning(open_app):
    app = open_app(token="eski-jeton")
    assert app.wait_sent("hello")["token"] == "eski-jeton"
    app.wait_ready()
    app.wait_until(lambda: app.page.evaluate("localStorage.getItem('meetbot_admin_token')") is None,
                   "geçersiz jetonun silinmesi")
    assert any("süresi dolmuş" in text for text in app.toasts())


@pytest.mark.browser
def test_expired_token_does_not_delete_a_newer_token_from_another_tab(open_app):
    app = open_app(token="eski-jeton", hold_hello=True)
    assert app.wait_sent("hello")["token"] == "eski-jeton"
    # hello yanıtı beklenirken başka bir sekme yönetici girişi yapıp yeni jetonu kaydetti
    app.page.evaluate("localStorage.setItem('meetbot_admin_token', 'yeni-jeton')")
    app.server.release_hello()
    app.wait_ready()
    app.wait_until(lambda: any("süresi dolmuş" in text for text in app.toasts()), "süre doldu uyarısı")
    assert app.page.evaluate("localStorage.getItem('meetbot_admin_token')") == "yeni-jeton"


@pytest.mark.browser
def test_admin_logout_clears_token(open_app):
    app = open_app(token="tok-123", welcome_msg=welcome(admin=True))

    def logout_responder(server, ws, msg):
        ws.send(json.dumps({"type": "session", "is_admin": False, "permissions": GUEST_PERMISSIONS}))
        return {"ok": True, "message": "Çıkış yapıldı"}

    app.server.responders["logout"] = logout_responder
    app.wait_ready()
    page = app.page
    expect(page.locator("#admin-toggle-text")).to_have_text("Yönetici")
    page.click("#admin-toggle-btn")
    app.wait_sent("logout")
    assert app.dialogs and "oturumu kapatılsın" in app.dialogs[-1]
    expect(page.locator("#admin-toggle-text")).to_have_text("Admin")
    assert page.evaluate("localStorage.getItem('meetbot_admin_token')") is None
    expect(page.locator("#btn-clear")).to_be_hidden()
    assert app.toasts() == ["🔒 Yönetici oturumu kapatıldı"]


# ──────────────────────────────────────────────────────────────
#  Şarkı ekleme
# ──────────────────────────────────────────────────────────────

@pytest.mark.browser
def test_add_waits_for_ack_and_keeps_query_on_error(open_app):
    app = open_app()
    app.server.responders["add"] = None   # ack'i test elle gönderecek
    app.wait_ready()
    page = app.page

    page.click("#add-song-btn")
    assert any("şarkı adı" in text for text in app.toasts())
    assert app.server.sent("add") == []

    page.fill("#yt-link-input", "tarkan kuzu kuzu")
    page.press("#yt-link-input", "Enter")
    first = app.wait_sent("add")
    assert first["query"] == "tarkan kuzu kuzu" and first["rid"]
    expect(page.locator("#add-song-btn")).to_be_disabled()
    expect(page.locator("#add-song-label")).to_have_text("Ekleniyor…")
    page.press("#yt-link-input", "Enter")
    page.wait_for_timeout(100)
    assert len(app.server.sent("add")) == 1, "yanıt beklenirken ikinci istek gitmemeli"

    app.server.ack(first["rid"], ok=False, message="Şarkı bulunamadı")
    expect(page.locator("#add-song-btn")).to_be_enabled()
    expect(page.locator("#yt-link-input")).to_have_value("tarkan kuzu kuzu")
    assert "❌ Şarkı bulunamadı" in app.toasts()

    page.click("#add-song-btn")
    second = app.wait_sent("add", 2)
    app.server.push({"type": "notice", "level": "success", "message": "🎵 Vedat: Kuzu Kuzu kuyruğa eklendi"})
    app.server.ack(second["rid"], ok=True, message="🎵 Kuzu Kuzu kuyruğa eklendi")
    expect(page.locator("#yt-link-input")).to_have_value("")
    expect(page.locator("#add-song-label")).to_have_text("Ekle")
    # Ekleyen kişi aynı olayı iki kez görmez: yayınlanan notice yeterli, ack mesajı gösterilmez
    assert [t for t in app.toasts() if "kuyruğa eklendi" in t] == ["🎵 Vedat: Kuzu Kuzu kuyruğa eklendi"]
    assert_no_errors(app)


@pytest.mark.browser
@pytest.mark.parametrize("max_duration, shown", [(1200, "20 dk"), (90, "1:30"), (20, "0:20"), (450, "7:30")])
def test_add_hint_shows_the_exact_max_duration(open_app, max_duration, shown):
    """MEETBOT_MAX_DURATION saniyedir: dakikaya yuvarlanırsa ipucu sunucunun reddiyle çelişir
    (90 sn → "2 dk" derken 100 sn'lik şarkı reddedilir; 20 sn → "0 dk")."""
    welcome_msg = welcome()
    welcome_msg["limits"]["max_duration"] = max_duration
    app = open_app(welcome_msg=welcome_msg)
    app.wait_ready()
    expect(app.page.locator("#add-hint")).to_have_text(
        f"Liste: en fazla 25 şarkı · Süre: en fazla {shown}")


# ──────────────────────────────────────────────────────────────
#  Güvenlik: sunucudan gelen metin asla HTML olarak yorumlanmaz
# ──────────────────────────────────────────────────────────────

@pytest.mark.browser
def test_untrusted_text_is_never_interpreted_as_html(open_app):
    evil = '"><img src=x onerror="window.__xss=1"><b onmouseover="window.__xss=2">x</b>'
    quoted = 'x" onmouseover="window.__xss=3" style="position:fixed;inset:0;z-index:9999" data-z="'
    state = snapshot(
        queue=[track(1, evil, added_by=evil), track(2, quoted), track(3, "Artist - \"Song\" 'Live' & <Co>")],
        current=track(4, evil, added_by=evil, thumbnail="javascript:alert(1)"),
        playback={"state": "playing", "position": 1, "duration": 200, "repeat": "off"},
        history=[track(5, evil, added_by=evil)],
        listeners=[evil, "Vedat"],
        bot={"status": "connected", "meet_link": MEET_LINK, "detail": evil},
    )
    app = open_app(welcome_msg=welcome(admin=True, state=state))
    app.wait_ready()
    page = app.page
    app.server.push({"type": "notice", "level": "info", "message": evil})
    app.server.push({"type": "error", "message": "Hata: " + evil})
    expect(page.locator("#toast-container .toast")).to_have_count(2)

    for locator in page.locator("#queue-list > li").all():
        locator.hover()
    page.mouse.move(640, 450)
    assert page.evaluate("window.__xss") is None
    assert page.locator("img[src='x']").count() == 0
    handlers = page.evaluate("""() => [...document.querySelectorAll('*')]
        .flatMap(el => [...el.attributes].map(a => a.name))
        .filter(name => name.startsWith('on'))""")
    assert handlers == []
    title = page.locator("#queue-list > li").nth(1).locator(".qi-body p").first
    expect(title).to_have_attribute("title", quoted)
    expect(title).to_have_text(quoted)
    expect(page.locator("#queue-list > li").nth(2).locator(".qi-body p").first) \
        .to_have_text("Artist - \"Song\" 'Live' & <Co>")
    expect(page.locator("#np-title")).to_have_text(evil)
    expect(page.locator("#np-thumb")).to_be_hidden()
    expect(page.locator("#bot-detail")).to_have_text(evil)
    assert any(evil in text for text in app.toasts())
    assert_no_errors(app)


# ──────────────────────────────────────────────────────────────
#  Kuyruk eylemleri
# ──────────────────────────────────────────────────────────────

def queue_button(page, index: int, action: str):
    return page.locator("#queue-list > li").nth(index).locator(f"button[data-action='{action}']")


@pytest.mark.browser
def test_queue_buttons_and_keyboard_send_move_play_now_remove(open_app):
    app = open_app(welcome_msg=welcome(admin=True, state=playing_state()))
    app.wait_ready()
    page = app.page
    expect(queue_button(page, 0, "up")).to_be_disabled()
    expect(queue_button(page, 2, "down")).to_be_disabled()
    expect(queue_button(page, 0, "remove")).to_have_attribute("aria-label", "Kuyruktan kaldır: Şarkı 1")

    queue_button(page, 0, "down").click()
    assert {k: v for k, v in app.wait_sent("move").items() if k != "rid"} == {"type": "move", "id": 1, "index": 1}
    queue_button(page, 2, "up").click()
    assert (app.wait_sent("move", 2)["id"], app.server.sent("move")[1]["index"]) == (3, 1)
    queue_button(page, 1, "play_now").click()
    assert app.wait_sent("play_now")["id"] == 2
    queue_button(page, 2, "remove").click()
    assert app.wait_sent("remove")["id"] == 3

    queue_button(page, 1, "down").focus()
    page.keyboard.press("Alt+ArrowUp")
    assert (app.wait_sent("move", 3)["id"], app.server.sent("move")[2]["index"]) == (2, 0)

    # Sunucu yeni sırayı yayınlayınca odak aynı şarkının butonunda kalır
    app.server.push({"type": "queue", "queue": [track(2), track(1, added_by="Vedat"), track(3, added_by="Mehmet")]})
    expect(page.locator("#queue-list > li").first).to_have_attribute("data-id", "2")
    assert page.evaluate("document.activeElement.dataset.id") == "2"
    assert_no_errors(app)


def reorder_responder(queue: list[dict]) -> Responder:
    """Gerçek sunucu gibi taşır: kuyruğu değiştirir, önce herkese "queue" yayını, sonra ack."""
    def respond(server: FakeServer, ws, msg: dict) -> dict:
        moved = next(item for item in queue if item["id"] == msg["id"])
        queue.remove(moved)
        queue.insert(max(0, min(msg["index"], len(queue))), moved)
        ws.send(json.dumps({"type": "queue", "queue": queue}))
        return {"ok": True}
    return respond


@pytest.mark.browser
def test_held_alt_arrow_and_rapid_clicks_are_coalesced_into_few_moves(open_app):
    """Sunucu ping dahil 30 mesaj / 10 sn kabul eder. Basılı tutulan Alt+↓ (~30/sn) ya da art arda
    tıklamalar her basışta bir "move" gönderirse sınır dolar, kalp atışı ping'i de "pong" yerine
    "error" alır. Yolda tek istek olur; beklerken gelen basışlar tek hedefte birleşir."""
    queue = [track(i) for i in range(1, 31)]
    app = open_app(welcome_msg=welcome(admin=True, state=playing_state(queue=list(queue))), clock=True)
    app.server.responders["move"] = reorder_responder(queue)
    app.wait_ready()
    page = app.page

    def sent_moves() -> list[tuple[int, int]]:
        return [(msg["id"], msg["index"]) for msg in app.server.sent("move")]

    page.locator("#queue-list button[data-action='down'][data-id='1']").focus()
    for _ in range(20):
        page.keyboard.press("Alt+ArrowDown")
    app.wait_sent("move")
    app.tick(600)
    app.wait_sent("move", 2)
    app.tick(600)
    assert sent_moves() == [(1, 1), (1, 20)], "ilk basış hemen, kalan 19 basış tek hedefte"
    expect(page.locator("#queue-list > li").nth(20)).to_have_attribute("data-id", "1")
    expect(page.locator("#queue-list button[data-id='1'][data-action='down']")).to_be_focused()

    up = page.locator("#queue-list button[data-action='up'][data-id='30']")
    for _ in range(10):
        up.click()
    app.wait_sent("move", 3)
    app.tick(600)
    app.wait_sent("move", 4)
    app.tick(600)
    assert sent_moves()[2:] == [(30, 28), (30, 19)]
    expect(page.locator("#queue-list > li").nth(19)).to_have_attribute("data-id", "30")
    assert [msg["id"] for msg in app.server.sent("move")] == [1, 1, 30, 30]
    assert_no_errors(app)


@pytest.mark.browser
def test_keyboard_focus_stays_in_the_queue_after_remove_and_play_now(open_app):
    """Odaklı satır listeden çıkınca (kaldır, "Şimdi çal") odak <body>'ye düşmemeli: klavye ve
    ekran okuyucu kullanıcısı 100 şarkılık listede yerini kaybeder."""
    app = open_app(welcome_msg=welcome(admin=True, state=playing_state(queue=[track(i) for i in range(1, 5)])))
    app.wait_ready()
    page = app.page

    def button(track_id: int, action: str):
        return page.locator(f"#queue-list button[data-id='{track_id}'][data-action='{action}']")

    button(2, "remove").focus()
    page.keyboard.press("Enter")
    assert app.wait_sent("remove")["id"] == 2
    app.server.push({"type": "queue", "queue": [track(1), track(3), track(4)]})
    expect(page.locator("#queue-list > li")).to_have_count(3)
    expect(button(3, "remove")).to_be_focused()        # aynı sıradaki satırın aynı butonu

    button(4, "play_now").focus()                         # son satır
    page.keyboard.press("Enter")
    assert app.wait_sent("play_now")["id"] == 4
    app.server.push({"type": "queue", "queue": [track(1), track(3)]})
    expect(page.locator("#queue-list > li")).to_have_count(2)
    expect(button(3, "play_now")).to_be_focused()      # altında satır kalmadı: bir üstteki

    page.keyboard.press("Enter")
    assert app.wait_sent("play_now", 2)["id"] == 3
    app.server.push({"type": "queue", "queue": []})
    expect(page.locator("#queue-empty")).to_be_visible()
    expect(page.locator("#queue-heading")).to_be_focused()   # liste boşaldı: kuyruk başlığı
    assert_no_errors(app)


DISPATCH_DRAG_JS = """([selector, type, mime, data]) => {
    const target = document.querySelector(selector);
    const transfer = new DataTransfer();
    transfer.setData(mime, data);
    const rect = target.getBoundingClientRect();
    target.dispatchEvent(new DragEvent(type, {
        bubbles: true, cancelable: true, dataTransfer: transfer,
        clientX: rect.left + 10, clientY: rect.top + rect.height - 2,
    }));
}"""


@pytest.mark.browser
def test_drag_and_drop_moves_by_id_and_ignores_foreign_drops(open_app):
    app = open_app(welcome_msg=welcome(admin=True, state=playing_state()))
    app.wait_ready()
    page = app.page
    items = page.locator("#queue-list > li")

    third = items.nth(2).bounding_box()
    items.nth(0).drag_to(items.nth(2), target_position={"x": 30, "y": third["height"] - 4})
    move = app.wait_sent("move")
    assert (move["id"], move["index"]) == (1, 2)
    items.nth(2).drag_to(items.nth(0), target_position={"x": 30, "y": 4})
    move = app.wait_sent("move", 2)
    assert (move["id"], move["index"]) == (3, 0)

    # Dışarıdan sürüklenen link/metin (eski sürümdeki bayat dragSrcEl hatası) hiçbir şey göndermemeli
    for event in ("dragover", "drop"):
        page.evaluate(DISPATCH_DRAG_JS, ["#queue-list > li:nth-child(2)", event, "text/uri-list",
                                         "https://www.youtube.com/watch?v=x"])
    page.wait_for_timeout(150)
    assert len(app.server.sent("move")) == 2

    # Sürükleme sürerken gelen kuyruk güncellemesi bırakınca çizilir
    page.evaluate(DISPATCH_DRAG_JS, ["#queue-list > li:nth-child(2)", "dragstart", "text/plain", ""])
    app.server.push({"type": "queue", "queue": playing_state()["queue"] + [track(4)]})
    page.wait_for_timeout(150)
    expect(items).to_have_count(3)
    page.evaluate(DISPATCH_DRAG_JS, ["#queue-list > li:nth-child(2)", "dragend", "text/plain", ""])
    expect(items).to_have_count(4)
    assert_no_errors(app)


@pytest.mark.browser
def test_queue_header_shows_count_eta_shuffle_and_clear(open_app):
    state = playing_state()
    state["queue"].append(track(4, duration=300, status="error"))
    state["queue"].append(track(5, duration=None, status="pending"))
    app = open_app(welcome_msg=welcome(admin=True, state=state))
    app.wait_ready()
    page = app.page
    expect(page.locator("#queue-count")).to_have_text("5")
    # 3 × 200 sn + çalan şarkıdan kalan 190 sn = 13:10; indirilemeyen şarkı atlanacağı için süreye eklenmez
    expect(page.locator("#queue-eta")).to_contain_text(re.compile(r"Kalan 13:(09|10) · bitiş ≈ \d\d:\d\d"))
    expect(page.locator("#queue-eta")).to_contain_text("+1 süresi bilinmeyen")
    expect(page.locator("#queue-list > li").nth(3)).to_contain_text("HATA")

    page.click("#btn-shuffle")
    app.wait_sent("shuffle")
    page.click("#btn-clear")
    app.wait_sent("clear")
    assert "silinsin mi" in app.dialogs[-1]

    app.server.push({"type": "queue", "queue": []})
    app.server.push({"type": "playback", "current": None, "state": "idle", "position": 0, "duration": 0, "repeat": "off"})
    expect(page.locator("#queue-empty")).to_be_visible()
    expect(page.locator("#queue-eta")).to_have_text("Kuyruk boş")
    expect(page.locator("#btn-clear")).to_be_disabled()


# ──────────────────────────────────────────────────────────────
#  Şu an çalan: ilerleme, konum, durumlar
# ──────────────────────────────────────────────────────────────

@pytest.mark.browser
def test_now_playing_card_progress_and_seek(open_app):
    app = open_app(welcome_msg=welcome(admin=True, state=playing_state()), clock=True)
    app.wait_ready()
    page = app.page
    expect(page.locator("#np-title")).to_have_text("Çalan Şarkı")
    expect(page.locator("#np-requester")).to_have_text("İsteyen: Zeynep")
    expect(page.locator("#np-thumb")).to_be_visible()
    assert page.locator("#np-thumb").get_attribute("src").startswith("https://i.ytimg.com/")
    expect(page.locator("#np-status")).to_contain_text("Oynatılıyor")
    expect(page.locator("#np-time")).to_have_text("0:10 / 3:20")
    assert "Çalan Şarkı" in page.title()

    app.server.push({"type": "progress", "position": 42, "duration": 200, "state": "playing"})
    expect(page.locator("#np-time")).to_have_text("0:42 / 3:20")
    app.tick(2_000)   # sunucu mesajları arasında yerel saat ilerlemeyi sürdürür
    expect(page.locator("#np-time")).to_have_text("0:44 / 3:20")

    page.evaluate("""() => {
        const seek = document.getElementById('np-seek');
        seek.value = '120';
        seek.dispatchEvent(new Event('input', { bubbles: true }));
        seek.dispatchEvent(new Event('change', { bubbles: true }));
    }""")
    app.tick(300)   # seçim dizisi bitince tek "seek" gider
    assert app.wait_sent("seek")["position"] == 120
    expect(page.locator("#np-time")).to_have_text("2:00 / 3:20")

    # Durdurulduktan sonra geç gelen ilerleme mesajı boş ekranı bozmamalı
    app.server.push({"type": "playback", "current": None, "state": "idle", "position": 0, "duration": 0, "repeat": "off"})
    expect(page.locator("#np-title")).to_have_text("Şarkı çalmıyor")
    app.server.push({"type": "progress", "position": 0, "duration": 225, "state": "playing"})
    page.wait_for_timeout(100)
    expect(page.locator("#np-time")).to_have_text("--:-- / --:--")
    expect(page.locator("#np-seek")).to_be_disabled()
    assert_no_errors(app)


@pytest.mark.browser
def test_seek_keyboard_hold_sends_one_message_and_failure_snaps_back(open_app):
    state = playing_state(playback={"state": "paused", "position": 10, "duration": 200, "repeat": "off"})
    app = open_app(welcome_msg=welcome(admin=True, state=state), clock=True)
    app.wait_ready()
    page = app.page
    time_text = page.locator("#np-time")
    page.locator("#np-seek").focus()
    for _ in range(10):   # basılı tutulan ok tuşu: her basış input + change üretir
        page.keyboard.press("ArrowRight")
        app.tick(30)
    expect(time_text).to_have_text("0:20 / 3:20")
    assert app.server.sent("seek") == [], "tuşlar bırakılmadan seek gönderilmemeli"
    app.tick(300)
    app.wait_sent("seek")
    page.wait_for_timeout(100)
    assert [msg["position"] for msg in app.server.sent("seek")] == [20]
    expect(time_text).to_have_text("0:20 / 3:20")

    app.server.responders["seek"] = lambda server, ws, msg: {"ok": False, "message": "Şarkı henüz yükleniyor"}
    for _ in range(3):
        page.keyboard.press("ArrowRight")
    expect(time_text).to_have_text("0:23 / 3:20")
    app.tick(300)
    app.wait_sent("seek", 2)
    expect(time_text).to_have_text("0:20 / 3:20")   # reddedilince bilinen gerçek konuma döner
    assert "❌ Şarkı henüz yükleniyor" in app.toasts()


@pytest.mark.browser
def test_track_change_cancels_a_pending_seek(open_app):
    state = playing_state(playback={"state": "paused", "position": 10, "duration": 200, "repeat": "off"})
    app = open_app(welcome_msg=welcome(admin=True, state=state), clock=True)
    app.wait_ready()
    page = app.page
    page.locator("#np-seek").focus()
    page.keyboard.press("End")
    expect(page.locator("#np-time")).to_have_text("3:20 / 3:20")
    app.server.push({"type": "playback", "current": track(2), "state": "paused", "position": 5, "duration": 200,
                     "repeat": "off"})
    expect(page.locator("#np-time")).to_have_text("0:05 / 3:20")
    app.tick(400)
    page.wait_for_timeout(100)
    assert app.server.sent("seek") == [], "önceki şarkı için seçilen konum yeni şarkıya uygulanmamalı"


@pytest.mark.browser
@pytest.mark.parametrize("selector, frac", [("#np-seek", 0.85), ("#music-volume-slider", 0.1)],
                         ids=["konum", "muzik-sesi"])
def test_touch_scroll_that_starts_on_a_slider_changes_nothing(open_app, selector, frac):
    """Telefonda sayfayı kaydıran parmak kaydırıcıya denk gelirse Blink değeri parmağın yerine atlatır
    ("input"), kaydırma başlayınca "pointercancel", parmak kalkınca yine de "change" gönderir. Misafir
    farkına bile varmadan toplantıdaki herkes için şarkıyı sardırıyor / sesi değiştiriyordu."""
    state = playing_state(playback={"state": "paused", "position": 20, "duration": 200, "repeat": "off"})
    app = open_app(welcome_msg=welcome(state=state), viewport=(390, 844), touch=True)
    app.wait_ready()
    page = app.page
    expect(page.locator(selector)).to_be_enabled()
    page.evaluate(RANGE_EVENT_LOG_JS)
    page.locator(selector).evaluate("el => el.scrollIntoView({ block: 'center' })")
    page.wait_for_timeout(200)
    scroll_before = page.evaluate("window.scrollY")

    touch_gesture(page, selector, frac, [(0, -14)] * 15)   # parmak yukarı: sayfa aşağı kayar
    page.wait_for_timeout(800)   # konum (300 ms) ve ses (400 ms) gecikmelerinden sonra da bir şey gitmemeli

    # Düzenek: sayfa gerçekten kaydı ve tarayıcı kaydırıcının değerini gerçekten değiştirdi
    assert page.evaluate("window.scrollY") - scroll_before > 50, "sayfa kaymadı"
    events = page.evaluate("window.__rangeEvents")
    name = selector.lstrip("#")
    assert f"{name}:input" in events and f"{name}:pointercancel" in events, events

    assert app.server.sent("seek") == [] and app.server.sent("volume") == []
    expect(page.locator("#np-time")).to_have_text("0:20 / 3:20")
    expect(page.locator("#np-seek")).to_have_value("20")
    expect(page.locator("#music-volume-slider")).to_have_value("80")
    expect(page.locator("#music-volume-value")).to_have_text("80%")

    # Kaydırma bitti: sunucudan gelen güncellemeler kaydırıcıya yine yansır (etkileşim askıda kalmadı)
    app.server.push({"type": "progress", "position": 42, "duration": 200, "state": "paused"})
    app.server.push({"type": "volume", "music": 35, "mic": 80})
    expect(page.locator("#np-time")).to_have_text("0:42 / 3:20")
    expect(page.locator("#music-volume-value")).to_have_text("35%")
    assert_no_errors(app)


@pytest.mark.browser
def test_touch_tap_and_horizontal_drag_on_sliders_still_work(open_app):
    state = playing_state(playback={"state": "paused", "position": 20, "duration": 200, "repeat": "off"})
    app = open_app(welcome_msg=welcome(state=state), viewport=(390, 844), touch=True)
    app.wait_ready()
    page = app.page

    # Konum çubuğunun ortasına dokunmak oraya sarar
    page.locator("#np-seek").evaluate("el => el.scrollIntoView({ block: 'center' })")
    touch_gesture(page, "#np-seek", 0.5, [])
    assert 90 <= app.wait_sent("seek")["position"] <= 110
    page.wait_for_timeout(400)
    assert len(app.server.sent("seek")) == 1

    # Yatay sürükleme sesi değiştirir (sürerken kısıtlı gönderim, bırakınca son değer)
    slider = page.locator("#music-volume-slider")
    slider.evaluate("el => el.scrollIntoView({ block: 'center' })")
    page.wait_for_timeout(100)
    scroll_before = page.evaluate("window.scrollY")
    touch_gesture(page, "#music-volume-slider", 0.1, [(12, 0)] * 15)
    app.wait_until(lambda: app.server.sent("volume")
                   and str(app.server.sent("volume")[-1]["value"]) == slider.input_value(),
                   "son ses değerinin gönderilmesi")
    final = int(slider.input_value())
    assert final >= 50
    expect(page.locator("#music-volume-value")).to_have_text(f"{final}%")
    assert page.evaluate("window.scrollY") == scroll_before, "yatay sürükleme sayfayı kaydırmamalı"
    assert all(msg["target"] == "music" for msg in app.server.sent("volume"))
    assert_no_errors(app)


@pytest.mark.browser
def test_drag_that_ends_on_its_start_value_does_not_freeze_the_sliders(open_app):
    """İleri sürükleyip aynı değere geri bırakınca tarayıcı "change" göndermez. Etkileşim yalnızca
    "change" ile bitiyorsa konum çubuğu şarkı değişene kadar donar, ses kaydırıcısı da başka
    kullanıcıların değişikliklerini göstermez."""
    state = playing_state(playback={"state": "paused", "position": 10, "duration": 20, "repeat": "off"},
                          volume={"music": 50, "mic": 80})
    app = open_app(welcome_msg=welcome(admin=True, state=state))
    app.wait_ready()
    page = app.page

    def middle_of(selector: str) -> tuple[float, float]:
        page.locator(selector).evaluate("el => el.scrollIntoView({ block: 'center' })")
        box = page.locator(selector).bounding_box()
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    def drag_away_and_back(x: float, y: float, offset: int) -> list[str]:
        page.evaluate(RANGE_EVENT_LOG_JS)
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + offset, y, steps=5)
        page.mouse.move(x, y, steps=5)
        page.mouse.up()
        page.wait_for_timeout(50)
        return page.evaluate("window.__rangeEvents")

    # Konum 20 sn'lik şarkının tam ortasında (10): başparmağın merkezi kutunun ortasıdır
    events = drag_away_and_back(*middle_of("#np-seek"), 80)
    assert "np-seek:input" in events and "np-seek:change" not in events, events
    app.server.push({"type": "progress", "position": 15, "duration": 20, "state": "paused"})
    expect(page.locator("#np-time")).to_have_text("0:15 / 0:20")
    expect(page.locator("#np-seek")).to_have_value("15")
    page.wait_for_timeout(400)
    assert app.server.sent("seek") == [], "konum değişmedi, seek gönderilmemeli"

    # Ses 0-100: bir birim ~3 px. Önce kaydırıcıyı o pikselin değerine oturt (bu sürükleme "change" üretebilir)
    slider = page.locator("#music-volume-slider")
    x, y = middle_of("#music-volume-slider")
    drag_away_and_back(x, y, -60)
    start = int(slider.input_value())
    app.wait_until(lambda: app.server.volume["music"] == start, "hazırlık değerinin sunucuya ulaşması")
    page.wait_for_timeout(600)   # son isteğin yanıtı da işlensin
    before = len(app.server.sent("volume"))

    events = drag_away_and_back(x, y, -60)
    assert "music-volume-slider:input" in events and "music-volume-slider:change" not in events, events
    # Sürerken giden ara değer düzeltilir: sunucuda yine başlangıç değeri kalır
    app.wait_until(lambda: len(app.server.sent("volume")) > before and app.server.volume["music"] == start,
                   "son ses değerinin gönderilmesi")
    page.wait_for_timeout(600)
    expect(slider).to_be_focused()
    app.server.push({"type": "volume", "music": 20, "mic": 80})   # başka bir kullanıcı değiştirdi
    expect(slider).to_have_value("20")
    expect(page.locator("#music-volume-value")).to_have_text("20%")
    assert_no_errors(app)


@pytest.mark.browser
def test_now_playing_thumbnail_falls_back_to_icon_when_it_fails(open_app):
    app = open_app(welcome_msg=welcome(state=playing_state()))
    app.wait_ready()
    page = app.page
    page.route("https://i.ytimg.com/vi/kirik/**", lambda route: route.fulfill(status=404, body=""))
    broken = track(10, "Kapaksız Şarkı", thumbnail="https://i.ytimg.com/vi/kirik/mqdefault.jpg")
    playback = {"type": "playback", "current": broken, "state": "playing", "position": 0, "duration": 200,
                "repeat": "off"}
    app.server.push(playback)
    expect(page.locator("#np-title")).to_have_text("Kapaksız Şarkı")
    expect(page.locator("#np-art-icon")).to_be_visible()
    expect(page.locator("#np-thumb")).to_be_hidden()
    # Aynı şarkının sonraki güncellemesi kırık görseli yeniden açmaz; yeni şarkının kapağı yine denenir
    app.server.push({**playback, "state": "paused"})
    expect(page.locator("#np-status")).to_contain_text("Duraklatıldı")
    expect(page.locator("#np-thumb")).to_be_hidden()
    app.server.push({**playback, "current": track(11)})
    expect(page.locator("#np-thumb")).to_be_visible()
    expect(page.locator("#np-art-icon")).to_be_hidden()
    assert [e for e in app.errors if "status of 404" not in e] == []


@pytest.mark.browser
def test_loading_state_is_distinct_and_cancellable(open_app):
    state = playing_state(playback={"state": "loading", "position": 0, "duration": 200, "repeat": "off"},
                          bot={"status": "disconnected", "meet_link": None, "detail": None})
    app = open_app(welcome_msg=welcome(admin=True, state=state))
    app.wait_ready()
    page = app.page
    expect(page.locator("#np-status")).to_contain_text("Yükleniyor")
    expect(page.locator("#btn-playpause")).to_be_disabled()
    expect(page.locator("#btn-playpause")).to_have_attribute("aria-label", "Yükleniyor")
    expect(page.locator("#btn-stop")).to_be_enabled()
    expect(page.locator("#btn-skip")).to_be_enabled()
    expect(page.locator("#np-hint")).to_contain_text("Bot toplantıya katılınca")
    page.click("#btn-skip")
    app.wait_sent("skip")
    page.click("#btn-stop")
    app.wait_sent("stop")


@pytest.mark.browser
def test_play_pause_toggle_and_repeat_cycle(open_app):
    app = open_app(welcome_msg=welcome(admin=True, state=playing_state()))
    app.wait_ready()
    page = app.page
    expect(page.locator("#btn-playpause")).to_have_attribute("aria-label", "Duraklat")
    page.click("#btn-playpause")
    app.wait_sent("pause")
    app.server.push({"type": "playback", "current": track(9), "state": "paused", "position": 12, "duration": 200,
                     "repeat": "off"})
    expect(page.locator("#np-status")).to_contain_text("Duraklatıldı")
    page.click("#btn-playpause")
    app.wait_sent("resume")

    modes = []
    for expected_next, label, icon_name in (("one", "Şarkı", "repeat_one"), ("all", "Liste", "repeat"),
                                            ("off", "Kapalı", "repeat")):
        page.click("#btn-repeat")
        sent = app.wait_sent("repeat", len(modes) + 1)
        modes.append(sent["mode"])
        assert sent["mode"] == expected_next
        app.server.push({"type": "playback", "current": track(9), "state": "paused", "position": 12,
                         "duration": 200, "repeat": expected_next})
        expect(page.locator("#repeat-label")).to_have_text(f"Tekrar: {label}")
        expect(page.locator("#btn-repeat-icon")).to_have_text(icon_name)
    assert modes == ["one", "all", "off"]


# ──────────────────────────────────────────────────────────────
#  Geçmiş, dinleyenler, ses
# ──────────────────────────────────────────────────────────────

@pytest.mark.browser
def test_history_readd_and_listeners(open_app):
    history = [track(7, "Eski Şarkı", added_by="Mehmet"), track(8)]
    app = open_app(welcome_msg=welcome(state=snapshot(history=history)))
    app.wait_ready()
    page = app.page
    expect(page.locator("#history-list > li")).to_have_count(2)
    expect(page.locator("#history-empty")).to_be_hidden()
    page.locator("#history-list button[data-action='readd']").first.click()
    sent = app.wait_sent("add")
    assert sent["query"] == "https://www.youtube.com/watch?v=vid00000007" and sent["rid"]

    app.server.push({"type": "listeners", "listeners": ["Ayşe", "Mehmet", "Vedat"], "count": 3})
    assert app.toasts() == [], "ekleme onayı ayrıca bildirim olarak gösterilmemeli"
    expect(page.locator("#listeners-count")).to_have_text("3")
    expect(page.locator("#listeners-list > li")).to_have_count(3)
    expect(page.locator("#listeners-list > li").nth(2)).to_have_text("Vedat (sen)")
    app.server.push({"type": "history", "history": []})
    expect(page.locator("#history-empty")).to_be_visible()
    assert_no_errors(app)


@pytest.mark.browser
def test_history_readd_state_follows_the_track_when_history_shifts(open_app):
    history = [track(7, "Eski Şarkı"), track(8, "Daha Eski")]
    app = open_app(welcome_msg=welcome(state=snapshot(history=history)))
    app.server.responders["add"] = None   # ack'i test elle gönderecek
    app.wait_ready()
    page = app.page
    button = page.locator("#history-list button[data-id='7']")
    button.click()
    sent = app.wait_sent("add")
    expect(button).to_have_attribute("aria-busy", "true")
    expect(button).to_be_focused()   # "disabled" olmadığı için klavye odağı kaybolmaz

    # Beklerken yeni bir şarkı bitti: satırlar kaydı, bekleme durumu yine aynı şarkıda
    app.server.push({"type": "history", "history": [track(12, "Yeni Biten"), *history]})
    expect(page.locator("#history-list > li")).to_have_count(3)
    expect(page.locator("#history-list button[aria-busy='true']")).to_have_attribute("data-id", "7")
    expect(button).to_be_focused()
    button.dispatch_event("click")   # beklerken tekrar tıklamak ikinci istek göndermez
    page.wait_for_timeout(100)
    assert len(app.server.sent("add")) == 1

    app.server.ack(sent["rid"], ok=True)
    expect(page.locator("#history-list button[aria-busy='true']")).to_have_count(0)
    assert_no_errors(app)


@pytest.mark.browser
def test_volume_slider_is_throttled_and_follows_remote_changes(open_app):
    app = open_app(welcome_msg=welcome(admin=True, state=playing_state()), clock=True)
    app.wait_ready()
    page = app.page
    slider = page.locator("#music-volume-slider")
    slider.focus()
    for value in range(60, 40, -2):   # 10 input olayı, 50 ms arayla (toplam 500 ms)
        slider.evaluate("(el, v) => { el.value = String(v); el.dispatchEvent(new Event('input', {bubbles: true})); }",
                        value)
        expect(page.locator("#music-volume-value")).to_have_text(f"{value}%")
        app.tick(50)
    slider.evaluate("el => el.dispatchEvent(new Event('change', {bubbles: true}))")
    app.tick(400)   # son değer de kısıtlamaya uyar
    app.wait_until(lambda: app.server.sent("volume") and app.server.sent("volume")[-1]["value"] == 42,
                   "son ses değerinin gönderilmesi")
    sent = app.server.sent("volume")
    assert [msg["value"] for msg in sent] == [60, 46, 42], "400 ms'de en fazla bir mesaj + son değer"
    assert all(msg["target"] == "music" for msg in sent)

    # Basılı tutulan ok tuşu da (her basışta input + change) mesaj yağmuru üretmez
    before = len(app.server.sent("volume"))
    for _ in range(10):
        page.keyboard.press("ArrowRight")
        app.tick(30)
    app.tick(400)
    after = app.server.sent("volume")[before:]
    assert 1 <= len(after) <= 3 and after[-1]["value"] == 52

    # Kaydırıcı odakta kalsa bile başka kullanıcının değişikliği görünür
    expect(slider).to_be_focused()
    app.server.push({"type": "volume", "music": 93, "mic": 80})
    expect(slider).to_have_value("93")
    expect(page.locator("#music-volume-value")).to_have_text("93%")


@pytest.mark.browser
def test_volume_slider_shows_server_value_after_failed_or_slow_request(open_app):
    app = open_app(welcome_msg=welcome(admin=True, state=playing_state()), clock=True)
    app.server.responders["volume"] = None   # yayın yok, ack'i test elle gönderecek
    app.wait_ready()
    page = app.page
    slider = page.locator("#music-volume-slider")
    slider.evaluate("""el => {
        el.value = '30';
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    }""")
    first = app.wait_sent("volume")
    assert first["value"] == 30
    app.tick(500)   # bırakma işlendi ama ilk istek hâlâ yanıt bekliyor
    expect(page.locator("#music-volume-value")).to_have_text("30%")
    assert len(app.server.sent("volume")) == 1, "aynı değer ikinci kez gönderilmemeli"

    app.server.ack(first["rid"], ok=False, message="Ses seviyesi uygulanamadı")
    expect(slider).to_have_value("80")   # sunucudaki gerçek değer
    expect(page.locator("#music-volume-value")).to_have_text("80%")
    assert "❌ Ses seviyesi uygulanamadı" in app.toasts()


@pytest.mark.browser
def test_mic_toggle_reflects_server_state(open_app):
    app = open_app(welcome_msg=welcome(admin=True, state=playing_state()))
    app.wait_ready()
    page = app.page
    button = page.locator("#btn-toggle-mic")
    expect(button).to_have_attribute("aria-pressed", "false")
    button.click()
    assert app.wait_sent("mic")["muted"] is True
    app.server.push({"type": "mic", "muted": True})
    expect(button).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#mic-text")).to_have_text("Mikrofon KAPALI")


# ──────────────────────────────────────────────────────────────
#  Bot durumu ve Meet kontrolleri
# ──────────────────────────────────────────────────────────────

@pytest.mark.browser
def test_bot_status_detail_cancel_and_link_handling(open_app):
    app = open_app(welcome_msg=welcome(admin=True))
    app.wait_ready()
    page = app.page
    expect(page.locator("#status-text")).to_have_text("Bağlı Değil")

    page.fill("#meet-link-input", "merhaba")
    page.click("#meet-join-btn")
    assert any("Geçerli bir Google Meet linki" in text for text in app.toasts())
    page.fill("#meet-link-input", "ABC-defg-HIJ")
    page.press("#meet-link-input", "Enter")
    assert app.wait_sent("join_meet")["link"] == MEET_LINK

    app.server.push({"type": "bot", "status": "connecting", "meet_link": MEET_LINK,
                     "detail": "Katılma isteği gönderildi, onay bekleniyor"})
    expect(page.locator("#status-text")).to_have_text("Bağlanıyor…")
    expect(page.locator("#bot-detail")).to_have_text("Katılma isteği gönderildi, onay bekleniyor")
    expect(page.locator("#meet-join-btn")).to_be_hidden()
    dialogs_before = len(app.dialogs)
    page.click("#meet-cancel-btn")
    app.wait_sent("leave_meet")
    assert len(app.dialogs) == dialogs_before, "İptal onay sormamalı"

    app.server.push({"type": "bot", "status": "connected", "meet_link": MEET_LINK, "detail": "Toplantıya katıldı"})
    expect(page.locator("#meet-join-btn")).to_have_text("Değiştir")
    expect(page.locator("#meet-leave-btn")).to_be_visible()
    expect(page.locator("#meet-link-input")).to_have_value(MEET_LINK)
    page.press("#meet-link-input", "Enter")
    page.wait_for_timeout(100)
    assert len(app.server.sent("join_meet")) == 1, "aynı link için yeniden katılma gönderilmemeli"
    assert any("zaten bu toplantıda" in text for text in app.toasts())

    pasted = "https://meet.google.com/XYZ-abcd-efg?authuser=0 https://meet.google.com/XYZ-abcd-efg"
    page.fill("#meet-link-input", pasted)
    page.click("#meet-join-btn")
    assert app.wait_sent("join_meet", 2)["link"] == "https://meet.google.com/xyz-abcd-efg"
    assert "yeni toplantıya" in app.dialogs[-1]

    page.click("#meet-leave-btn")
    app.wait_sent("leave_meet", 2)
    assert "ayrılsın mı" in app.dialogs[-1]
    assert_no_errors(app)


@pytest.mark.browser
def test_meet_input_keeps_typed_text_and_restores_link_when_emptied(open_app):
    state = snapshot(bot={"status": "connected", "meet_link": MEET_LINK, "detail": None})
    app = open_app(welcome_msg=welcome(admin=True, state=state))
    app.wait_ready()
    page = app.page
    meet = page.locator("#meet-link-input")
    expect(meet).to_have_value(MEET_LINK)
    meet.fill("https://meet.google.com/xyz-")

    # Başka bir yönetici botu taşıdı: yazılmakta olan metin ezilmez
    other = "https://meet.google.com/qwe-rtyu-iop"
    app.server.push({"type": "bot", "status": "connected", "meet_link": other, "detail": "Toplantıya katıldı"})
    expect(page.locator("#bot-detail")).to_have_text("Toplantıya katıldı")
    expect(meet).to_have_value("https://meet.google.com/xyz-")

    # Boşaltılıp bırakılınca botun bulunduğu toplantının linki geri gelir
    meet.fill("")
    meet.blur()
    expect(meet).to_have_value(other)
    assert app.server.sent("join_meet") == []


# ──────────────────────────────────────────────────────────────
#  Bağlantı: yeniden bağlanma, kalp atışı, sağlamlık
# ──────────────────────────────────────────────────────────────

@pytest.mark.browser
def test_reconnect_uses_exponential_backoff_with_banner_and_disabled_controls(open_app):
    app = open_app(welcome_msg=welcome(admin=True, state=playing_state()), clock=True)
    app.wait_ready()
    page = app.page
    banner = page.locator("#conn-banner-text")

    app.server.push({"type": "progress", "position": 42, "duration": 200, "state": "playing"})
    expect(page.locator("#np-time")).to_have_text("0:42 / 3:20")
    app.server.close()
    expect(banner).to_have_text("Sunucu bağlantısı koptu — 1 sn içinde yeniden denenecek…")
    # Canlı bölgede yalnızca durum metni var; her saniye değişen geri sayım ekran okuyucudan gizli
    expect(page.locator("#conn-banner")).to_have_attribute("role", "status")
    expect(page.locator("#conn-banner-status")).to_have_text("Sunucu bağlantısı koptu")
    expect(page.locator("#conn-banner-countdown")).to_have_attribute("aria-hidden", "true")
    expect(page.locator("#btn-playpause")).to_be_disabled()
    expect(page.locator("#add-song-btn")).to_be_disabled()
    expect(page.locator("#queue-list button[data-action]").first).to_be_disabled()

    app.server.refuse_connections = True
    app.tick(990)
    assert len(app.server.connections) == 1, "1 sn dolmadan yeniden denenmemeli"
    for expected_delay in (2, 4, 8):
        attempts = len(app.server.connections)
        app.tick(60)   # önceki bekleme doldu → yeni deneme → sunucu reddeder → bekleme ikiye katlanır
        app.wait_connections(attempts + 1)
        expect(banner).to_have_text(f"Sunucu bağlantısı koptu — {expected_delay} sn içinde yeniden denenecek…")
        app.tick(expected_delay * 1_000 - 50)
        assert len(app.server.connections) == attempts + 1, f"{expected_delay} sn dolmadan yeniden denenmemeli"
    attempts = len(app.server.connections)
    # Sunucuya ulaşılamazken yerel saat şarkıyı ilerletmiş gibi göstermez
    expect(page.locator("#np-time")).to_have_text("0:42 / 3:20")

    # "Şimdi dene" beklemeyi atlar; bağlanınca bant kaybolur ve deneme sayacı sıfırlanır
    app.server.refuse_connections = False
    page.click("#conn-retry-btn")
    app.wait_connections(attempts + 1)
    app.wait_ready()
    expect(page.locator("#conn-banner")).to_be_hidden()
    expect(page.locator("#btn-playpause")).to_be_enabled()
    assert "✅ Sunucu bağlantısı yeniden kuruldu" in app.toasts()
    assert not any("koptu" in text for text in app.toasts()), "kopma için bildirim yağmuru olmamalı"

    app.server.close()
    expect(banner).to_have_text("Sunucu bağlantısı koptu — 1 sn içinde yeniden denenecek…")


@pytest.mark.browser
def test_heartbeat_detects_a_dead_connection(open_app):
    app = open_app(clock=True)
    app.wait_ready()
    # Ağ geri gelince (uyku, Wi-Fi değişimi) yarı açık bağlantı beklemeden yoklanır
    app.page.evaluate("window.dispatchEvent(new Event('online'))")
    app.wait_sent("ping")
    app.server.answer_pings = False
    pings = len(app.server.sent("ping"))
    app.tick(25_000)
    assert len(app.server.sent("ping")) == pings + 1
    expect(app.page.locator("#conn-banner")).to_be_hidden()
    app.tick(10_000)
    expect(app.page.locator("#conn-banner")).to_be_visible()


@pytest.mark.browser
def test_any_server_frame_answers_the_heartbeat(open_app):
    """Hız sınırına takılan ping "pong" değil rid'siz "error" alır. Yalnızca "pong" beklenirse sağlam
    bağlantı 10 sn sonra "pong yanıtı gelmedi" diye koparılır; sunucudan gelen her çerçeve canlılık kanıtıdır."""
    app = open_app(clock=True)
    app.wait_ready()
    banner = app.page.locator("#conn-banner")
    app.server.answer_pings = False
    app.page.evaluate("window.dispatchEvent(new Event('online'))")
    app.wait_sent("ping")
    app.server.push({"type": "error", "message": "Çok hızlı mesaj gönderiyorsun, biraz yavaşla"})
    app.tick(11_000)
    expect(banner).to_be_hidden()
    assert len(app.server.connections) == 1, "sağlam bağlantı koparılmamalı"
    assert "✅ Sunucu bağlantısı yeniden kuruldu" not in app.toasts()

    # Hiçbir şey gelmezse ölü bağlantı yine yakalanır
    app.tick(14_000)   # 25 sn: sıradaki kalp atışı
    app.wait_sent("ping", 2)
    app.tick(10_000)
    expect(banner).to_be_visible()


@pytest.mark.browser
def test_command_queued_behind_a_slow_operation_is_not_reported_as_timed_out(open_app):
    """Sunucu bir bağlantının komutlarını sırayla yürütür: "atla", botun toplantıdan ayrılmasının (~18 sn)
    ya da şarkı yüklemesinin (en fazla 45 sn) arkasında bekleyebilir. 15 sn'de "zamanında yanıt vermedi"
    demek yanlıştı (işlem ardından yine gerçekleşir); gecikince yalnızca "sırada" bilgisi gösterilir."""
    app = open_app(welcome_msg=welcome(admin=True, state=playing_state()), clock=True)
    app.server.responders["skip"] = None   # ack'i test elle gönderecek
    app.wait_ready()
    page = app.page
    notice = "⏳ Sunucu önceki işlemleri bitiriyor, isteğin sırada…"

    page.click("#btn-skip")
    first = app.wait_sent("skip")
    app.tick(14_000)
    assert app.toasts() == []
    app.tick(2_000)
    assert app.toasts() == [notice]
    app.tick(20_000)   # 36 sn: hâlâ bekliyor
    app.server.ack(first["rid"], ok=True, message="⏭️ Atlandı")
    expect(page.locator("#toast-container .toast-text", has_text="Atlandı")).to_be_visible()
    assert not any("zamanında" in text for text in app.toasts())

    # Sunucu gerçekten hiç yanıt vermezse sonunda yine hata gösterilir
    page.click("#btn-skip")
    app.wait_sent("skip", 2)
    app.tick(60_000)
    assert "❌ Sunucu zamanında yanıt vermedi" in app.toasts()

    # Katıl/ayrıl doğası gereği uzun sürer (bot durumu görünür): "sırada" bildirimi gösterilmez
    app.server.responders["leave_meet"] = None
    app.tick(10_000)   # önceki bildirimler kaybolsun
    page.click("#meet-leave-btn")
    app.wait_sent("leave_meet")
    app.tick(20_000)
    assert notice not in app.toasts()
    assert_no_errors(app)


@pytest.mark.browser
def test_malformed_messages_do_not_break_the_ui(open_app):
    app = open_app()
    app.wait_ready()
    page = app.page
    for raw in ("bu json değil", json.dumps({"type": "constructor"}), json.dumps({"no": "type"}),
                json.dumps({"type": "queue", "queue": "bozuk"}), json.dumps({"type": "bot", "status": "??"}),
                json.dumps({"type": "ack", "rid": "bilinmeyen", "ok": True})):
        app.server.push(raw)
    app.server.push({"type": "queue", "queue": [track(1)]})
    expect(page.locator("#queue-list > li")).to_have_count(1)
    expect(page.locator("#status-text")).to_have_text("Bağlı Değil")
    assert [e for e in app.errors if "Geçersiz JSON" not in e] == []
    assert not any(e.startswith("pageerror") for e in app.errors)


@pytest.mark.browser
def test_toasts_are_deduplicated_and_capped(open_app):
    app = open_app(clock=True)   # bildirim süreleri dolmasın
    app.wait_ready()
    page = app.page
    for _ in range(3):
        app.server.push({"type": "notice", "level": "warning", "message": "⚠️ Aynı uyarı"})
    toast = page.locator("#toast-container .toast")
    expect(toast).to_have_count(1)
    expect(toast.locator(".toast-count")).to_have_text("×3")
    for index in range(6):
        app.server.push({"type": "notice", "level": "info", "message": f"Bildirim {index}"})
    expect(toast).to_have_count(4)
    expect(toast.last).to_contain_text("Bildirim 5")
    toast.last.locator(".toast-close").click()
    expect(toast).to_have_count(3)


# ──────────────────────────────────────────────────────────────
#  Yerleşim ve kaynaklar
# ──────────────────────────────────────────────────────────────

NO_OVERFLOW_JS = "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"


@pytest.mark.browser
@pytest.mark.parametrize("viewport", [(390, 844), (1440, 900)], ids=["mobil", "masaustu"])
def test_layout_has_no_horizontal_scroll_and_reachable_controls(open_app, viewport):
    state = playing_state(history=[track(7), track(8)], listeners=["Ayşe", "Mehmet", "Vedat"])
    state["queue"].append(track(4, "Çok " * 40 + "uzun başlık", added_by="Adı Çok Uzun Bir Kullanıcı"))
    app = open_app(welcome_msg=welcome(admin=True, state=state), viewport=viewport)
    app.wait_ready()
    page = app.page
    page.wait_for_timeout(200)
    assert page.evaluate(NO_OVERFLOW_JS), "yatay kaydırma çubuğu oluştu"

    width = viewport[0]
    boxes = page.eval_on_selector_all(
        "#app button:not([hidden]), #app input", """els => els
            .filter(el => el.offsetParent !== null)
            .map(el => { const r = el.getBoundingClientRect(); return [el.id || el.dataset.action || el.tagName, r.left, r.right]; })""")
    outside = [box for box in boxes if box[1] < -1 or box[2] > width + 1]
    assert not outside, f"ekran dışında kalan kontroller: {outside}"

    # Kuyruk eylemleri dokunmatikte de görünür (hover'a bağlı değil) ve yeterince büyük
    actions = page.eval_on_selector_all("#queue-list button[data-action]:not([disabled])", """els => els.map(el => {
        const r = el.getBoundingClientRect();
        return [Number(getComputedStyle(el).opacity), r.width, r.height];
    })""")
    assert actions and all(opacity >= 0.8 and w >= 36 and h >= 36 for opacity, w, h in actions)
    assert_no_errors(app)


def css_alpha(color: str) -> float:
    """getComputedStyle renginin opaklığı: "rgba(0, 0, 0, 0.6)" → 0.6, "rgb(…)" → 1."""
    parts = re.findall(r"[\d.]+", color)
    return float(parts[3]) if len(parts) == 4 else 1.0


@pytest.mark.browser
def test_listener_chips_and_history_rows_stay_readable_over_the_statue(open_app):
    """Sağ sütun (yarı saydam .v-window) geniş dizüstü ekranlarda dekoratif heykelin açık lavanta
    lekesinin üstüne gelir; kendi adının fuşya yazısı neredeyse okunmaz oluyordu. Metnin arkasında
    koyu bir zemin olmalı."""
    state = snapshot(history=[track(7)], listeners=["Ayşe", "Vedat"])
    app = open_app(welcome_msg=welcome(state=state), viewport=(1280, 720))
    app.wait_ready()
    page = app.page
    expect(page.locator("#listeners-list > li").nth(1)).to_have_text("Vedat (sen)")
    backgrounds = page.eval_on_selector_all("#listeners-list > li, #history-list > li",
                                            "els => els.map(el => getComputedStyle(el).backgroundColor)")
    assert len(backgrounds) == 3
    assert all(css_alpha(color) >= 0.6 for color in backgrounds), backgrounds


@pytest.mark.browser
def test_decorative_statue_is_hidden_when_it_fails_to_load(open_app):
    app = open_app(statue_status=404)
    app.wait_ready()
    expect(app.page.locator("#deco-statue")).to_have_count(0)


@pytest.mark.browser
def test_decorative_statue_is_kept_when_it_loads(open_app):
    app = open_app()
    app.wait_ready()
    statue = app.page.locator("#deco-statue")
    expect(statue).to_be_visible()
    expect(statue).to_have_attribute("alt", "")
    assert len(app.statue_requests) == 1
    assert_no_errors(app)


@pytest.mark.browser
def test_decorative_statue_is_not_downloaded_on_small_screens(open_app):
    app = open_app(viewport=(390, 844))
    app.wait_ready()
    app.page.wait_for_timeout(300)
    expect(app.page.locator("#deco-statue")).to_be_hidden()
    assert app.statue_requests == [], "telefonda görünmeyen dekoratif görsel indirilmemeli (loading=lazy)"


def open_everything(open_app) -> App:
    """Arayüzün olabildiğince çok parçasını çizdirir (ikon alt kümesi ve CSS sınıfı denetimleri için)."""
    state = snapshot(
        queue=[track(1, status="ready"), track(2, status="downloading"), track(3, status="error"),
               track(4, status="pending")],
        current=track(9),
        playback={"state": "playing", "position": 5, "duration": 200, "repeat": "one"},
        mic_muted=True,
        history=[track(7)],
        bot={"status": "connected", "meet_link": MEET_LINK, "detail": "Toplantıya katıldı"},
        listeners=["Ayşe", "Vedat"],
    )
    app = open_app(welcome_msg=welcome(state=state))   # önce misafir (kilit ikonu)
    app.wait_ready()
    page = app.page
    app.server.push({"type": "session", "is_admin": True, "permissions": ADMIN_PERMISSIONS})
    expect(page.locator("#admin-toggle-icon")).to_have_text("lock_open")
    app.server.push({"type": "playback", "current": track(9), "state": "loading", "position": 0, "duration": 200,
                     "repeat": "all"})
    app.server.push({"type": "playback", "current": track(9), "state": "paused", "position": 5, "duration": 200,
                     "repeat": "all"})
    app.server.push({"type": "notice", "level": "info", "message": "merhaba"})
    app.server.push({"type": "notice", "level": "error", "message": "hata"})
    expect(page.locator("#toast-container .toast")).to_have_count(2)
    return app


@pytest.mark.browser
def test_icon_font_subset_matches_every_rendered_icon(open_app):
    app = open_everything(open_app)
    rendered = set(app.page.evaluate("[...window.__icons]")) - {""}
    listed = set(icon_subset())
    assert rendered - listed == set(), f"icon_names listesinde eksik ikonlar: {sorted(rendered - listed)}"
    assert listed - rendered == set(), f"icon_names listesinde kullanılmayan ikonlar: {sorted(listed - rendered)}"


# Sayfadaki her sınıf için yüklü stil sayfalarında en az bir kural var mı? (eksik olanları döndürür)
UNSTYLED_CLASSES_JS = r"""() => {
    const selectors = [];
    const collect = (rules) => {
        for (const rule of rules) {
            if (rule.selectorText) selectors.push(rule.selectorText);
            if (rule.cssRules) collect(rule.cssRules);
        }
    };
    for (const sheet of document.styleSheets) {
        // Yabancı kökenli sayfaların (Google Fonts) kuralları okunamaz ve bizim sınıflarımızı içermez
        if (sheet.href && new URL(sheet.href).origin !== location.origin) continue;
        collect(sheet.cssRules);
    }
    const all = selectors.join("\n");
    const escapeRe = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const classes = new Set();
    document.querySelectorAll("[class]").forEach((node) => node.classList.forEach((name) => classes.add(name)));
    return [...classes]
        .filter((name) => !new RegExp("\\." + escapeRe(CSS.escape(name)) + "(?![\\w-])").test(all))
        .sort();
}"""


@pytest.mark.browser
def test_every_rendered_class_has_a_css_rule(open_app):
    """Derlenmiş static/tailwind.css güncel mi: HTML'de ya da app.js'te kullanılan her sınıf CSS'te tanımlı olmalı."""
    app = open_everything(open_app)
    missing = set(app.page.evaluate(UNSTYLED_CLASSES_JS))
    # Bağlantı bandı, body.is-offline ve devre dışı kontroller
    app.server.close()
    expect(app.page.locator("#conn-banner")).to_be_visible()
    missing |= set(app.page.evaluate(UNSTYLED_CLASSES_JS))
    assert not missing, f"CSS kuralı olmayan sınıflar ('npm run build:css' çalıştırın): {sorted(missing)}"



# ──────────────────────────────────────────────────────────────
#  Bot ekranı (yönetici): botun tarayıcısını panelden görme / kullanma
# ──────────────────────────────────────────────────────────────

VIEW_PERMISSIONS = ["view_start", "view_stop", "view_input", "google_logout"]


def view_png() -> str:
    from fake_bot import placeholder_png  # 16:9 yer tutucu kare (sunucu bunu gönderir)
    return "data:image/png;base64," + base64.b64encode(placeholder_png(160, 90)).decode("ascii")


def view_frame(**overrides) -> dict:
    frame = {"type": "view_frame", "target": "meet", "image": view_png(), "width": 1600, "height": 900,
             "url": MEET_LINK, "title": "Meet", "signed_in": False}
    frame.update(overrides)
    frame.setdefault("interactive", frame["target"] == "login")
    return frame


def view_inputs(app: App) -> list[dict]:
    return [{k: v for k, v in m.items() if k not in ("type", "rid")} for m in app.server.sent("view_input")]


@pytest.mark.browser
def test_bot_view_button_is_admin_only_and_explains_missing_permission(open_app):
    app = open_app()
    app.wait_ready()
    page = app.page
    expect(page.locator("#view-btn")).to_be_hidden()

    # Yönetici ama uzaktan bağlı (MEETBOT_REMOTE_VIEW=local): düğme görünür ama kapalı, ipucu SSH tünelini anlatır
    app.server.push({"type": "session", "is_admin": True, "permissions": ADMIN_PERMISSIONS})
    button = page.locator("#view-btn")
    expect(button).to_be_visible()
    expect(button).to_be_disabled()
    expect(button).to_have_attribute("title", re.compile("ssh -L 8000:127.0.0.1:8000"))

    app.server.push({"type": "session", "is_admin": True, "permissions": ADMIN_PERMISSIONS + VIEW_PERMISSIONS})
    expect(button).to_be_enabled()
    app.server.push({"type": "session", "is_admin": False, "permissions": GUEST_PERMISSIONS})
    expect(button).to_be_hidden()
    assert_no_errors(app)


@pytest.mark.browser
def test_bot_view_streams_frames_and_forwards_clicks_keys_and_text(open_app):
    app = open_app(welcome_msg=welcome(admin=True, permissions=ADMIN_PERMISSIONS + VIEW_PERMISSIONS,
                                       state=playing_state()))
    app.wait_ready()
    page = app.page
    page.click("#view-btn")
    expect(page.locator("#view-dialog")).to_be_visible()
    assert app.wait_sent("view_start")["target"] == "meet"
    expect(page.locator("#view-placeholder")).to_be_visible()
    expect(page.locator("#view-screen")).to_be_focused()

    # Meet sekmesi: yalnızca izleme — metin kutusu / tuşlar gizli, tıklama ve klavye bota gitmez
    app.server.push(view_frame(signed_in=False))
    image = page.locator("#view-img")
    expect(image).to_be_visible()
    expect(page.locator("#view-url")).to_have_text(MEET_LINK)
    expect(page.locator("#view-google")).to_have_text("Google: oturum kapalı")
    expect(page.locator("#view-tab-meet")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#view-type-form")).to_be_hidden()
    expect(page.locator("#view-readonly")).to_be_visible()
    expect(page.locator("#view-back-btn")).to_be_disabled()
    box = image.bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.keyboard.type("xyz")
    page.wait_for_timeout(600)
    assert app.server.sent("view_input") == []

    # Google girişi sekmesi: girdi açık
    page.click("#view-tab-login")
    assert app.wait_sent("view_start", 2)["target"] == "login"
    app.server.push(view_frame(target="login", url="https://accounts.google.com/"))
    expect(page.locator("#view-tab-login")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#view-type-form")).to_be_visible()
    expect(page.locator("#view-readonly")).to_be_hidden()

    # Görüntünün ortasına tıklama → oran olarak (0.5, 0.5)
    box = image.bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    app.wait_sent("view_input")
    click = view_inputs(app)[0]
    assert click["action"] == "click"
    assert abs(click["x"] - 0.5) < 0.02 and abs(click["y"] - 0.5) < 0.02

    # Ekran seçiliyken yazılan harfler toplu "type", özel tuşlar "key" olarak ve SIRAYLA gider
    expect(page.locator("#view-screen")).to_be_focused()
    page.keyboard.type("ab c")
    page.keyboard.press("Enter")
    page.keyboard.press("Escape")   # pencereyi kapatmaz, bota gider
    app.wait_sent("view_input", 4)
    assert view_inputs(app)[1:4] == [{"action": "type", "text": "ab c"}, {"action": "key", "key": "Enter"},
                                     {"action": "key", "key": "Escape"}]
    expect(page.locator("#view-dialog")).to_be_visible()
    page.keyboard.press("Shift+Tab")
    app.wait_sent("view_input", 5)
    assert view_inputs(app)[4] == {"action": "key", "key": "Shift+Tab"}

    # Gizli metin kutusu (telefon için) + tuş düğmeleri + geri/yenile
    page.fill("#view-text", "gizli-şifre")
    page.click("#view-send-btn")
    page.click("#view-type-form button[data-key='Tab']")
    page.click("#view-back-btn")
    page.click("#view-reload-btn")
    app.wait_sent("view_input", 9)
    assert view_inputs(app)[5:9] == [{"action": "type", "text": "gizli-şifre"}, {"action": "key", "key": "Tab"},
                                     {"action": "back"}, {"action": "reload"}]
    expect(page.locator("#view-text")).to_have_value("")

    # Hata bildirimi
    app.server.push({"type": "view_error", "message": "Bot ekranı alınamadı"})
    expect(page.locator("#view-status")).to_contain_text("Bot ekranı alınamadı")

    # Oturum açıldı: sunucu giriş sekmesini kapatıp Meet sekmesini yollar → giriş sekmesi kilitlenir
    app.server.push(view_frame(target="meet", signed_in=True))
    expect(page.locator("#view-status")).to_contain_text("Google hesabı bağlandı")
    expect(page.locator("#view-google")).to_have_text("Google: oturum açık ✓")
    expect(page.locator("#view-tab-login")).to_be_disabled()
    expect(page.locator("#view-type-form")).to_be_hidden()
    signout = page.locator("#view-signout-btn")
    expect(signout).to_be_visible()
    signout.click()   # onay penceresi düzenekte otomatik kabul edilir
    app.wait_sent("google_logout")
    assert any("Google hesabından çıkış" in d for d in app.dialogs)
    expect(page.locator("#view-status")).to_contain_text("çıkış yapıldı")

    page.click("#view-close-btn")
    expect(page.locator("#view-overlay")).to_be_hidden()
    app.wait_sent("view_stop")
    assert page.locator("#view-img").get_attribute("src") is None
    expect(page.locator("#view-btn")).to_be_focused()
    # Yazılan şifre konsola / depolamaya düşmedi
    assert "gizli-şifre" not in json.dumps(page.evaluate("({...localStorage, ...sessionStorage})"))
    assert_no_errors(app)


@pytest.mark.browser
def test_bot_view_ignores_non_image_frames_and_closes_on_logout(open_app):
    app = open_app(welcome_msg=welcome(admin=True, permissions=ADMIN_PERMISSIONS + VIEW_PERMISSIONS))
    app.wait_ready()
    page = app.page
    page.click("#view-btn")
    app.wait_sent("view_start")
    for bad in ("javascript:alert(1)", "data:text/html;base64,PHNjcmlwdD4=", "https://evil.example/x.png",
                'data:image/png;base64,AAAA" onerror="alert(1)'):
        app.server.push(view_frame(image=bad))
    app.server.push({"type": "ping_marker"})
    page.wait_for_timeout(100)
    expect(page.locator("#view-img")).to_be_hidden()
    assert page.locator("#view-img").get_attribute("src") is None

    # Yönetici oturumu kapandı → pencere kapanır
    app.server.push({"type": "session", "is_admin": False, "permissions": GUEST_PERMISSIONS})
    expect(page.locator("#view-overlay")).to_be_hidden()
    expect(page.locator("#view-btn")).to_be_hidden()
    assert_no_errors(app)


@pytest.mark.browser
def test_bot_view_resumes_after_reconnect(open_app):
    welcome_msg = welcome(admin=True, permissions=ADMIN_PERMISSIONS + VIEW_PERMISSIONS)
    app = open_app(welcome_msg=welcome_msg, clock=True)
    app.wait_ready()
    page = app.page
    page.click("#view-btn")
    page.click("#view-tab-login")
    app.wait_sent("view_start", 2)
    app.server.close()
    expect(page.locator("#view-status")).to_contain_text("bağlantısı koptu")
    app.tick(1_010)
    app.wait_connections(2)
    assert app.wait_sent("view_start", 3)["target"] == "login"   # sunucu izleyiciyi unuttu: yeniden abone ol
    expect(page.locator("#view-dialog")).to_be_visible()
    assert_no_errors(app)
