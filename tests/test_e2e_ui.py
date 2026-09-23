"""Uçtan uca arayüz testleri: GERÇEK sunucu + GERÇEK arayüz, iki kullanıcı aynı anda.

• Sunucu: server.create_app + gerçek Player ve Hub; bot yerine FakeBot (Chrome yok),
  indirici yerine tests/fakes.py'deki FakeDownloader (ağ yok, kısa WAV dosyaları).
  uvicorn arka plandaki bir iş parçacığında, boş bir localhost portunda çalışır.
• Arayüz: static/ dosyaları sunucudan gelir; Playwright'ın Chromium'unda İKİ ayrı bağlam
  (yönetici + misafir) aynı anda açıktır. Dış kaynaklar (font, küçük resim) yerel yanıtla
  karşılanır; test internete çıkmaz.
• Sunucu durumu testten, uvicorn'un olay döngüsünde çalışan küçük çağrılarla okunur/değiştirilir.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import pytest
import uvicorn
from fastapi import FastAPI

import main
from audio_manager import ResolveError, canonical_watch_url
from fake_bot import FakeBot
from fakes import FakeDownloader, make_info
from player import Player
from server import Hub, create_app

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

TRACK_SECONDS = 60          # FakeDownloader'ın yazdığı WAV'ların süresi (= katalogdaki süre)
MEET_LINK = "https://meet.google.com/abc-defg-hij"
OTHER_LINK = "https://meet.google.com/xyz-wxyz-xyz"
PASSWORD = "test-secret"    # conftest'teki settings fixture'ının yönetici şifresi
XSS_TITLE = '"><img src=x onerror=window.__xss=1>'
XSS_NAME = "<img src=x onerror=__xss=2>"     # 27 karakter: isim sınırına (32) sığar
ADMIN_ONLY = "Bu işlem için yönetici yetkisi gerekiyor"
NO_GUEST_CONTROLS = "Bu işlem için yetkin yok (misafir kontrolleri kapalı)"
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


# ──────────────────────────────────────────────────────────────
#  Gerçek sunucu (uvicorn, arka plan iş parçacığı)
# ──────────────────────────────────────────────────────────────

class _LoopServer(uvicorn.Server):
    """Olay döngüsünü saklayan uvicorn sunucusu (testler ona iş gönderebilsin)."""

    loop: asyncio.AbstractEventLoop | None = None

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        self.loop = asyncio.get_running_loop()
        await super().startup(sockets=sockets)


class LiveServer:
    """main.py'deki kablolamanın aynısı: bot → indirici → Hub → Player → create_app."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.bot = FakeBot(settings, speed=1.0, tick=0.25, join_delay=0.3)
        self.downloader = FakeDownloader(settings.downloads_dir, wav_seconds=TRACK_SECONDS)
        self.hub = Hub()
        self.player = Player(settings, self.bot, self.downloader, self.hub.broadcast)
        self.bot.on_status = self.player.on_bot_status
        self.bot.on_track_ended = self.player.on_track_ended
        self.bot.on_progress = self.player.on_progress
        app = create_app(settings, self.player, self.bot, self.hub)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))   # boş port; soket uvicorn'a verilir (yarış yok)
        self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        # main.serve() ile aynı ayarlar; yalnızca kapanış beklemesi kısa (bkz. test_main_bounds_...).
        config = uvicorn.Config(app, log_config=None, access_log=False, lifespan="on",
                                ws_max_size=main.WS_MAX_SIZE, timeout_graceful_shutdown=2)
        self.server = _LoopServer(config)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]},
                                       name=f"e2e-uvicorn:{self.port}", daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("uvicorn başlatılamadı")
            time.sleep(0.02)

    def call(self, fn: Callable[..., Any], *args: Any, timeout: float = 10.0) -> Any:
        """fn'i sunucunun olay döngüsünde çalıştırır (awaitable dönerse bekler) ve sonucu verir."""
        async def runner():
            result = fn(*args)
            if inspect.isawaitable(result):
                result = await result
            return result

        return asyncio.run_coroutine_threadsafe(runner(), self.server.loop).result(timeout)

    def add_to_catalog(self, query: str, video_id: str, title: str) -> None:
        info = make_info(video_id, title, TRACK_SECONDS)

        def register():
            self.downloader.catalog[query] = [info]
            # Geçmişten yeniden ekleme parçanın kanonik bağlantısını gönderir
            self.downloader.catalog[canonical_watch_url(video_id)] = [info]

        self.call(register)

    def queue_titles(self) -> list[str]:
        return self.call(lambda: [t.title for t in self.player.queue])

    def wait_for(self, predicate: Callable[[], bool], what: str, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not self.call(predicate):
            if time.monotonic() > deadline:
                raise AssertionError(f"Zaman aşımı: {what}")
            time.sleep(0.05)

    def drain(self, timeout: float = 3.0) -> None:
        """Tarayıcılar kapandıktan sonra açık bağlantıların bitmesini bekler. uvicorn 0.53 kapanırken
        kapanmakta olan bir WebSocket'e yeniden close göndermeye çalışırsa (websockets InvalidState)
        Server.shutdown() patlar ve lifespan kapanışı (player/bot.shutdown) hiç çalışmaz."""
        deadline = time.monotonic() + timeout
        while self.call(lambda: len(self.server.server_state.connections)) and time.monotonic() < deadline:
            time.sleep(0.05)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=30)
        assert not self.thread.is_alive(), "uvicorn kapanmadı"


# ──────────────────────────────────────────────────────────────
#  Tarayıcı tarafı
# ──────────────────────────────────────────────────────────────

def fulfill_external(route) -> None:
    """Dış kaynaklar (Google Fonts, i.ytimg.com küçük resimleri, dekoratif görsel) yerelden."""
    if "fonts.googleapis.com" in route.request.url:
        route.fulfill(status=200, body="", content_type="text/css")
    elif route.request.resource_type == "image":
        route.fulfill(status=200, body=PNG_1PX, content_type="image/png")
    else:
        route.fulfill(status=200, body="")


@dataclass
class User:
    context: Any
    page: Any
    errors: list[str] = field(default_factory=list)
    dialogs: list[str] = field(default_factory=list)

    def login(self, name: str) -> None:
        """İlk ziyaret: isim ekranı → uygulama; welcome gelince kontroller açılır."""
        page = self.page
        expect(page.locator("#login-overlay")).to_be_visible()
        page.fill("#login-username", name)
        page.click("#login-btn")
        self.wait_ready()
        expect(page.locator("#user-badge-name")).to_have_text(name)

    def wait_ready(self) -> None:
        expect(self.page.locator("#login-overlay")).to_be_hidden()
        expect(self.page.locator("#admin-toggle-btn")).to_be_enabled()

    def login_admin(self, password: str = PASSWORD) -> None:
        page = self.page
        page.click("#admin-toggle-btn")
        expect(page.locator("#admin-modal-overlay")).to_be_visible()
        page.fill("#admin-password", password)
        page.click("#admin-login-btn")
        expect(page.locator("#admin-modal-overlay")).to_be_hidden()
        expect(page.locator("#admin-toggle-text")).to_have_text("Yönetici")

    def join_meet(self, text: str) -> None:
        self.page.fill("#meet-link-input", text)
        self.page.click("#meet-join-btn")

    def add(self, query: str, *, enter: bool = False) -> None:
        self.page.fill("#yt-link-input", query)
        if enter:
            self.page.press("#yt-link-input", "Enter")
        else:
            self.page.click("#add-song-btn")

    def toast(self, text: str):
        return self.page.locator("#toast-container .toast-text", has_text=text)

    def queue_titles(self):
        return self.page.locator("#queue-list > li p.font-display")

    def history_titles(self):
        return self.page.locator("#history-list > li p.font-display")

    def queue_button(self, index: int, action: str):
        return self.page.locator("#queue-list > li").nth(index).locator(f"button[data-action='{action}']")

    def force_click(self, element_id: str) -> None:
        """Arayüzün kapattığı/gizlediği bir düğmeyi zorla tıklar (mesaj yine de sunucuya gider)."""
        self.page.evaluate("""(id) => {
            const button = document.getElementById(id);
            button.hidden = false;
            button.disabled = false;
            button.click();
        }""", element_id)

    def assert_no_xss(self) -> None:
        assert self.page.evaluate("() => typeof window.__xss") == "undefined"
        assert self.page.locator("img[src='x'], [onerror]").count() == 0


class E2E:
    """Test başına bir gerçek sunucu + istenen sayıda tarayıcı bağlamı; hepsini düzgün kapatır."""

    def __init__(self, browser, settings) -> None:
        self.browser = browser
        self.settings = settings
        self.live: LiveServer | None = None
        self.users: list[User] = []

    def start(self, *, guest_controls: bool = True) -> LiveServer:
        self.settings.guest_controls = guest_controls
        self.live = LiveServer(self.settings)
        return self.live

    def open(self, *, name: str | None = None, clock: bool = False) -> User:
        """Yeni bir tarayıcı bağlamı (ayrı localStorage). name verilirse isim önceden kayıtlıdır."""
        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        context.route(re.compile(r"^https://"), fulfill_external)
        page = context.new_page()
        page.set_default_timeout(8000)
        user = User(context, page)
        self.users.append(user)
        page.on("console", lambda m: user.errors.append(f"console.{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda exc: user.errors.append(f"pageerror: {exc}"))

        def on_dialog(dialog) -> None:
            user.dialogs.append(dialog.message)
            dialog.accept()

        page.on("dialog", on_dialog)
        if name is not None:
            page.add_init_script(script=f"localStorage.setItem('meetbot_username', {json.dumps(name)});")
        if clock:
            page.clock.install()
        page.goto(self.live.url)
        return user

    def close(self) -> None:
        # Önce sayfalar (yeniden bağlanma denemeleri olmasın), sonra sunucu (lifespan kapanışı)
        for user in self.users:
            try:
                user.context.close()
            except Exception:  # pragma: no cover - kapanışta tarayıcı çoktan gitmiş olabilir
                pass
        if self.live is not None:
            self.live.drain()
            self.live.stop()


@pytest.fixture(scope="module")
def ui_browser():
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
def e2e(ui_browser, settings):
    env = E2E(ui_browser, settings)
    yield env
    env.close()


def assert_no_errors(*users: User) -> None:
    errors = [error for user in users for error in user.errors]
    assert errors == [], "\n".join(errors)


# ──────────────────────────────────────────────────────────────
#  Yolculuklar
# ──────────────────────────────────────────────────────────────

@pytest.mark.browser
def test_admin_and_guest_full_journey(e2e):
    live = e2e.start(guest_controls=True)
    first_url = "https://www.youtube.com/watch?v=vidBirinci1"
    third_url = "https://youtu.be/vidUcuncu01"
    live.add_to_catalog(first_url, "vidBirinci1", "Birinci Şarkı")
    live.add_to_catalog("lofi gece", "vidLofiGece", "Lofi Gece")
    live.add_to_catalog("zararlı başlık", "vidZararli1", XSS_TITLE)
    live.add_to_catalog(third_url, "vidUcuncu01", "Üçüncü Şarkı")

    # ── İsimle giriş (iki ayrı tarayıcı, aynı anda) ──────────
    admin = e2e.open()
    admin.login("Vedat")
    guest = e2e.open()
    guest.login("Ayşe")
    a, g = admin.page, guest.page

    for user in (admin, guest):
        expect(user.page.locator("#listeners-count")).to_have_text("2")
    expect(a.locator("#listeners-list > li")).to_have_text(["Ayşe", "Vedat (sen)"])
    expect(g.locator("#listeners-list > li")).to_have_text(["Ayşe (sen)", "Vedat"])

    # ── Kilit düğmesiyle yönetici girişi (sunucu doğrular, jeton saklanır) ──
    a.click("#admin-toggle-btn")
    a.fill("#admin-password", "yanlis-sifre")
    a.click("#admin-login-btn")
    expect(a.locator("#admin-error")).to_have_text("Şifre yanlış")
    a.fill("#admin-password", PASSWORD)
    a.click("#admin-login-btn")
    expect(a.locator("#admin-modal-overlay")).to_be_hidden()
    expect(a.locator("#admin-toggle-text")).to_have_text("Yönetici")
    expect(admin.toast("Yönetici girişi başarılı")).to_be_visible()
    token = a.evaluate("() => localStorage.getItem('meetbot_admin_token')")
    assert isinstance(token, str) and len(token) >= 24
    assert live.call(lambda: sorted((c.name, c.is_admin) for c in live.hub.clients)) == [
        ("Ayşe", False), ("Vedat", True)]
    expect(g.locator("#admin-toggle-text")).to_have_text("Admin")

    # ── Yetkiye göre arayüz (misafir kontrolleri AÇIK) ────────
    expect(g.locator("#meet-join-btn")).to_be_hidden()
    expect(g.locator("#btn-clear")).to_be_hidden()
    expect(g.locator("#btn-toggle-mic")).to_be_disabled()
    expect(g.locator("#btn-stop")).to_be_disabled()
    assert g.locator("#meet-link-input").evaluate("(el) => el.readOnly")
    expect(g.locator("#music-volume-slider")).to_be_enabled()
    expect(g.locator("#btn-repeat")).to_be_enabled()
    expect(a.locator("#meet-join-btn")).to_be_enabled()
    expect(a.locator("#btn-clear")).to_be_visible()
    expect(a.locator("#btn-toggle-mic")).to_be_enabled()

    # ── Meet: bağlanırken İptal, sonra katılma; durum + ayrıntı iki tarafta ──
    live.call(setattr, live.bot, "join_delay", 30.0)
    admin.join_meet("abc-defg-hij")   # çıplak toplantı kodu da kabul edilir
    for user in (admin, guest):
        expect(user.page.locator("#status-text")).to_have_text("Bağlanıyor…")
    expect(a.locator("#meet-cancel-btn")).to_be_visible()
    expect(g.locator("#meet-cancel-btn")).to_be_hidden()
    a.click("#meet-cancel-btn")
    for user in (admin, guest):
        expect(user.page.locator("#status-text")).to_have_text("Bağlı Değil")
        expect(user.page.locator("#bot-detail")).to_have_text("Katılma iptal edildi")
    expect(admin.toast("Katılma iptal edildi")).to_be_visible()

    live.call(setattr, live.bot, "join_delay", 0.3)
    admin.join_meet("https://meet.google.com/ABC-defg-hij?authuser=0 tekrar yapıştırıldı")
    for user in (admin, guest):
        expect(user.page.locator("#status-text")).to_have_text("Meet'te")
        expect(user.page.locator("#bot-detail")).to_have_text("Toplantıya katıldı (sahte bot)")
    expect(a.locator("#meet-link-input")).to_have_value(MEET_LINK)
    expect(a.locator("#meet-join-btn")).to_have_text("Değiştir")
    expect(a.locator("#meet-leave-btn")).to_be_visible()
    assert live.call(lambda: (live.bot.status, live.bot.meet_link)) == ("connected", MEET_LINK)

    # ── Bağlantıyla ekleme: düğme ack'i bekler; indirme sürerken YÜKLENİYOR ──
    live.call(lambda: live.downloader.download_gates.__setitem__("vidBirinci1", asyncio.Event()))
    live.call(setattr, live.downloader, "resolve_delay", 0.6)
    admin.add(first_url)
    expect(a.locator("#add-song-label")).to_have_text("Ekleniyor…")
    expect(a.locator("#add-song-btn")).to_be_disabled()
    expect(a.locator("#add-song-label")).to_have_text("Ekle")
    expect(a.locator("#yt-link-input")).to_have_value("")
    live.call(setattr, live.downloader, "resolve_delay", 0.0)
    for user in (admin, guest):
        expect(user.toast("🎵 Vedat: Birinci Şarkı kuyruğa eklendi")).to_be_visible()
        expect(user.page.locator("#np-title")).to_have_text("Birinci Şarkı")
        expect(user.page.locator("#np-status")).to_contain_text("Yükleniyor")
        expect(user.page.locator("#btn-playpause")).to_have_attribute("aria-label", "Yükleniyor")
        expect(user.page).to_have_title("⏳ Birinci Şarkı — MeetBot 3.2")
    assert live.call(lambda: live.player.state) == "loading"
    live.call(lambda: live.downloader.download_gates["vidBirinci1"].set())
    for user in (admin, guest):
        expect(user.page.locator("#np-status")).to_contain_text("Oynatılıyor")
        expect(user.page).to_have_title("▶ Birinci Şarkı — MeetBot 3.2")
        expect(user.page.locator("#np-requester")).to_have_text("İsteyen: Vedat")

    # ── Hatalı ekleme: hata ack'i gösterilir, yazılan korunur ─
    live.call(setattr, live.downloader, "resolve_error", ResolveError("Sadece YouTube bağlantıları destekleniyor"))
    admin.add("https://vimeo.com/12345")
    expect(admin.toast("❌ Sadece YouTube bağlantıları destekleniyor")).to_be_visible()
    expect(a.locator("#yt-link-input")).to_have_value("https://vimeo.com/12345")
    expect(a.locator("#add-song-label")).to_have_text("Ekle")
    expect(guest.toast("Sadece YouTube")).to_have_count(0)   # hata yalnızca isteyene gider
    live.call(setattr, live.downloader, "resolve_error", None)
    a.fill("#yt-link-input", "")

    # ── Misafir arama metniyle ekler; yönetici kuyruğu canlı görür ──
    guest.add("lofi gece", enter=True)
    expect(admin.queue_titles()).to_have_text(["Lofi Gece"])
    guest.add("zararlı başlık")
    expect(admin.queue_titles()).to_have_text(["Lofi Gece", XSS_TITLE])
    for user in (admin, guest):
        expect(user.toast(f"🎵 Ayşe: {XSS_TITLE} kuyruğa eklendi")).to_be_visible()   # bildirim düz metin
    admin.add(third_url)
    expect(guest.queue_titles()).to_have_text(["Lofi Gece", XSS_TITLE, "Üçüncü Şarkı"])
    expect(a.locator("#queue-count")).to_have_text("3")
    expect(g.locator("#queue-eta")).to_contain_text("Kalan")

    # ── Yukarı / aşağı düğmeleri (diğer bağlamda görünür) ─────
    admin.queue_button(0, "down").click()
    expect(guest.queue_titles()).to_have_text([XSS_TITLE, "Lofi Gece", "Üçüncü Şarkı"])
    guest.queue_button(2, "up").click()   # misafir kontrolleri açık → misafir de taşıyabilir
    expect(admin.queue_titles()).to_have_text([XSS_TITLE, "Üçüncü Şarkı", "Lofi Gece"])

    # ── HTML5 sürükle-bırak: ilk şarkı en sona ────────────────
    items = a.locator("#queue-list > li")
    last_box = items.nth(2).bounding_box()
    items.nth(0).drag_to(items.nth(2), source_position={"x": 12, "y": 12},
                         target_position={"x": 30, "y": last_box["height"] - 4})
    expect(guest.queue_titles()).to_have_text(["Üçüncü Şarkı", "Lofi Gece", XSS_TITLE])
    assert live.queue_titles() == ["Üçüncü Şarkı", "Lofi Gece", XSS_TITLE]

    # ── Tekrar modu döngüsü (misafir değiştirir, iki taraf görür) ──
    for label, mode in (("Şarkı", "one"), ("Liste", "all"), ("Kapalı", "off")):
        g.click("#btn-repeat")
        for user in (admin, guest):
            expect(user.page.locator("#repeat-label")).to_have_text(f"Tekrar: {label}")
        assert live.call(lambda: live.player.repeat) == mode

    # ── Atla → geçmiş paneli → tek tıkla yeniden ekle ─────────
    a.click("#btn-skip")
    for user in (admin, guest):
        expect(user.page.locator("#np-title")).to_have_text("Üçüncü Şarkı")
        expect(user.history_titles()).to_have_text(["Birinci Şarkı"])
    guest.page.locator("#history-list > li button[data-action='readd']").first.click()
    expect(admin.queue_titles()).to_have_text(["Lofi Gece", XSS_TITLE, "Birinci Şarkı"])
    assert live.call(lambda: live.player.queue[-1].added_by) == "Ayşe"

    # ── İlerleme çubuğuna tıklayarak konum seçme ──────────────
    live.wait_for(lambda: live.player.state == "playing", "Üçüncü Şarkı çalıyor")
    seek = a.locator("#np-seek")
    expect(seek).to_be_enabled()
    box = seek.bounding_box()
    seek.click(position={"x": box["width"] * 0.5, "y": box["height"] / 2})
    live.wait_for(lambda: 26 <= live.bot.position <= 36 and 26 <= live.player.position <= 36,
                  "sunucu ve bot yeni konumda")
    expect(g.locator("#np-time")).to_have_text(re.compile(r"^0:(2[6-9]|3\d) / 1:00$"))

    # ── Misafir kendi şarkısını kaldırır ──────────────────────
    expect(guest.queue_button(2, "remove")).to_have_attribute("aria-label", "Kuyruktan kaldır: Birinci Şarkı")
    guest.queue_button(2, "remove").click()
    expect(admin.queue_titles()).to_have_text(["Lofi Gece", XSS_TITLE])
    expect(guest.toast("Birinci Şarkı kuyruktan çıkarıldı")).to_be_visible()

    # ── Misafir yönetici düğmesini zorlarsa sunucu reddeder ───
    guest.force_click("btn-stop")
    expect(guest.toast(f"❌ {ADMIN_ONLY}")).to_be_visible()
    assert live.call(lambda: (live.player.state, live.player.current.title)) == ("playing", "Üçüncü Şarkı")

    # ── Zararlı başlık: çalan kart, sekme başlığı, geçmiş — hepsi düz metin ──
    guest.queue_button(1, "play_now").click()
    for user in (admin, guest):
        expect(user.page.locator("#np-title")).to_have_text(XSS_TITLE)
        expect(user.page.locator("#np-requester")).to_have_text("İsteyen: Ayşe")
        expect(user.page).to_have_title(f"▶ {XSS_TITLE} — MeetBot 3.2")
    a.click("#btn-skip")
    for user in (admin, guest):
        expect(user.page.locator("#np-title")).to_have_text("Lofi Gece")
        expect(user.history_titles()).to_have_text([XSS_TITLE, "Üçüncü Şarkı", "Birinci Şarkı"])
        user.assert_no_xss()

    # ── Sunucu tarafı kopma → otomatik yeniden bağlanma: yönetici kalır ──
    live.call(lambda: live.hub.drop([c for c in live.hub.clients if c.name == "Vedat"]))
    expect(a.locator("#conn-banner")).to_be_visible()
    expect(a.locator("#conn-banner")).to_be_hidden(timeout=10_000)
    expect(admin.toast("Sunucu bağlantısı yeniden kuruldu")).to_be_visible()
    expect(a.locator("#admin-toggle-text")).to_have_text("Yönetici")
    expect(a.locator("#btn-toggle-mic")).to_be_enabled()
    assert live.call(lambda: [c.is_admin for c in live.hub.clients if c.name == "Vedat"]) == [True]

    # ── Sayfa yenileme: isim ve yönetici jetonu kalıcı, durum anlık görüntüden gelir ──
    a.reload()
    admin.wait_ready()
    expect(a.locator("#user-badge-name")).to_have_text("Vedat")
    expect(a.locator("#admin-toggle-text")).to_have_text("Yönetici")
    expect(a.locator("#np-title")).to_have_text("Lofi Gece")
    expect(admin.history_titles()).to_have_text([XSS_TITLE, "Üçüncü Şarkı", "Birinci Şarkı"])
    for user in (admin, guest):
        expect(user.page.locator("#listeners-count")).to_have_text("2")
    expect(a.locator("#listeners-list > li")).to_have_text(["Ayşe", "Vedat (sen)"])

    # ── Doğal bitiş (sahte saat hızlandırılır): kuyruk boş → iki arayüz de beklemede ──
    live.call(setattr, live.bot, "speed", 60.0)
    for user in (admin, guest):
        expect(user.page.locator("#np-title")).to_have_text("Şarkı çalmıyor")
        expect(user.history_titles()).to_have_text(["Lofi Gece", XSS_TITLE, "Üçüncü Şarkı", "Birinci Şarkı"])
        expect(user.page).to_have_title("MeetBot 3.2 — Vaporwave Dreamscape")
        expect(user.page.locator("#queue-eta")).to_have_text("Kuyruk boş")
    assert live.call(lambda: (live.player.state, live.player.current)) == ("idle", None)

    admin.assert_no_xss()
    guest.assert_no_xss()
    assert_no_errors(admin, guest)


@pytest.mark.browser
def test_guest_without_controls_is_gated_and_forced_messages_are_rejected(e2e):
    live = e2e.start(guest_controls=False)
    live.add_to_catalog("yönetici şarkısı", "vidYonetic1", "Yönetici Şarkısı")
    live.add_to_catalog("ikinci şarkı", "vidIkinci01", "İkinci Şarkı")
    live.add_to_catalog("misafir şarkısı", "vidMisafir1", "Misafir Şarkısı")

    admin = e2e.open(name="Vedat")
    admin.wait_ready()
    admin.login_admin()
    guest = e2e.open(name=XSS_NAME)   # isim de sunucudan geri döner: düz metin kalmalı
    guest.wait_ready()
    a, g = admin.page, guest.page
    expect(g.locator("#user-badge-name")).to_have_text(XSS_NAME)

    admin.join_meet(MEET_LINK)
    expect(g.locator("#status-text")).to_have_text("Meet'te")
    admin.add("yönetici şarkısı")
    expect(g.locator("#np-title")).to_have_text("Yönetici Şarkısı")
    admin.add("ikinci şarkı")
    expect(g.locator("#queue-count")).to_have_text("1")
    guest.add("misafir şarkısı")
    expect(admin.queue_titles()).to_have_text(["İkinci Şarkı", "Misafir Şarkısı"])
    expect(admin.toast(f"🎵 {XSS_NAME}: Misafir Şarkısı kuyruğa eklendi")).to_be_visible()
    expect(a.locator("#queue-list > li").nth(1)).to_contain_text(XSS_NAME)   # "ekleyen" alanı
    expect(a.locator("#listeners-list > li")).to_have_text([XSS_NAME, "Vedat (sen)"])
    live.wait_for(lambda: live.player.state == "playing", "ilk şarkı çalıyor")

    # ── Misafirin kontrolleri kapalı / gizli ──────────────────
    for selector in ("#btn-playpause", "#btn-skip", "#btn-repeat", "#btn-stop", "#btn-shuffle",
                     "#music-volume-slider", "#mic-volume-slider", "#btn-toggle-mic", "#np-seek"):
        expect(g.locator(selector)).to_be_disabled()
    expect(g.locator("#btn-skip")).to_have_attribute("title", "Bu işlem için yönetici yetkisi gerekli")
    for selector in ("#btn-clear", "#meet-join-btn", "#meet-leave-btn", "#meet-cancel-btn"):
        expect(g.locator(selector)).to_be_hidden()
    guest_items = g.locator("#queue-list > li")
    expect(guest_items.nth(0).locator("button")).to_have_count(0)            # yöneticinin şarkısı
    expect(guest_items.nth(1).locator("button[data-action]")).to_have_count(1)
    expect(guest.queue_button(1, "remove")).to_be_enabled()                 # yalnızca kendi şarkısı
    assert guest_items.nth(0).get_attribute("draggable") is None
    # Yönetici her şeyi görür
    expect(a.locator("#queue-list > li").nth(1).locator("button[data-action]")).to_have_count(4)
    expect(a.locator("#btn-skip")).to_be_enabled()
    assert a.locator("#queue-list > li").nth(0).get_attribute("draggable") == "true"

    # ── Arayüzde zorlanan mesajlar sunucuda reddedilir ────────
    guest.force_click("btn-skip")
    expect(guest.toast(f"❌ {NO_GUEST_CONTROLS}")).to_be_visible()
    guest.force_click("btn-clear")   # onay penceresi kabul edilir; sunucu yine reddeder
    expect(guest.toast(f"❌ {ADMIN_ONLY}")).to_be_visible()

    # ── Ham WebSocket'ten gönderilen yasak mesajlar ───────────
    ids = live.call(lambda: {t.title: t.id for t in live.player.queue})
    forced = [
        ({"type": "skip"}, NO_GUEST_CONTROLS),
        ({"type": "pause"}, NO_GUEST_CONTROLS),
        ({"type": "seek", "position": 5}, NO_GUEST_CONTROLS),
        ({"type": "move", "id": ids["Misafir Şarkısı"], "index": 0}, NO_GUEST_CONTROLS),
        ({"type": "play_now", "id": ids["Misafir Şarkısı"]}, NO_GUEST_CONTROLS),
        ({"type": "volume", "target": "music", "value": 5}, NO_GUEST_CONTROLS),
        ({"type": "repeat", "mode": "all"}, NO_GUEST_CONTROLS),
        ({"type": "shuffle"}, NO_GUEST_CONTROLS),
        ({"type": "clear"}, ADMIN_ONLY),
        ({"type": "stop"}, ADMIN_ONLY),
        ({"type": "mic", "muted": True}, ADMIN_ONLY),
        ({"type": "join_meet", "link": OTHER_LINK}, ADMIN_ONLY),
        ({"type": "leave_meet"}, ADMIN_ONLY),
        ({"type": "remove", "id": ids["İkinci Şarkı"]}, "Sadece kendi eklediğin şarkıları kaldırabilirsin"),
    ]
    acks = g.evaluate(RAW_SOCKET_JS, [XSS_NAME, [message for message, _ in forced]])
    assert acks[0] == {"ok": True, "message": None}   # hello
    assert acks[1:] == [{"ok": False, "message": expected} for _, expected in forced]
    state = live.call(lambda: {
        "current": live.player.current.title, "state": live.player.state, "repeat": live.player.repeat,
        "queue": [t.title for t in live.player.queue], "volume": live.player.music_volume,
        "mic": live.player.mic_muted, "bot": (live.bot.status, live.bot.meet_link),
    })
    assert state == {
        "current": "Yönetici Şarkısı", "state": "playing", "repeat": "off",
        "queue": ["İkinci Şarkı", "Misafir Şarkısı"], "volume": 80, "mic": False,
        "bot": ("connected", MEET_LINK),
    }

    # ── İzin verilen: misafir kendi şarkısını kaldırır ────────
    guest.queue_button(1, "remove").click()
    expect(admin.queue_titles()).to_have_text(["İkinci Şarkı"])

    admin.assert_no_xss()
    guest.assert_no_xss()
    assert_no_errors(admin, guest)


@pytest.mark.browser
def test_slow_meet_leave_waits_for_its_ack_instead_of_timing_out(e2e):
    """Gerçek bot toplantıdan ayrılırken ~18 sn sürebilir; arayüz bunu zaman aşımı saymamalı."""
    live = e2e.start()
    admin = e2e.open(name="Vedat", clock=True)
    admin.wait_ready()
    admin.login_admin()
    a = admin.page
    admin.join_meet(MEET_LINK)
    expect(a.locator("#status-text")).to_have_text("Meet'te")

    gate = live.call(asyncio.Event)
    original_leave = live.bot.leave
    leaving = threading.Event()

    async def slow_leave() -> None:
        leaving.set()
        await gate.wait()
        await original_leave()

    live.call(setattr, live.bot, "leave", slow_leave)
    try:
        a.click("#meet-leave-btn")          # onay penceresi kabul edilir
        deadline = time.monotonic() + 5
        while not leaving.is_set():         # Playwright olayları (onay penceresi) işlensin diye sayfada bekle
            assert time.monotonic() < deadline, "leave_meet sunucuya ulaşmadı"
            a.wait_for_timeout(20)
        a.clock.fast_forward(20_000)        # sayfa saati 20 sn ileri: istek hâlâ yanıt bekliyor
        a.wait_for_timeout(300)
        assert admin.toast("zamanında yanıt vermedi").count() == 0
        expect(a.locator("#status-text")).to_have_text("Meet'te")
    finally:
        live.call(gate.set)                 # kapanışta bot.shutdown() de leave() çağırır
        live.call(delattr, live.bot, "leave")

    expect(admin.toast("Bot toplantıdan ayrıldı")).to_be_visible()
    expect(a.locator("#status-text")).to_have_text("Bağlı Değil")
    assert admin.toast("zamanında yanıt vermedi").count() == 0
    assert_no_errors(admin)


def test_main_bounds_the_graceful_shutdown(monkeypatch, settings):
    """Windows'ta (Proactor) tarayıcı bağlantıyı sert kapatınca (WinError 10054) asyncio sunucusu
    bağlantıyı "unutabilir" ve Server.wait_closed() hiç dönmez. Yukarıdaki E2E testlerinin kapanışında
    bu oluyor ("Cancel 0 running task(s), timeout graceful shutdown exceeded"); main.py uvicorn'a süre
    sınırı vermezse Ctrl+C'den sonra süreç "Shutting down"da sonsuza kadar asılı kalır."""
    seen = {}

    async def fake_serve(self, sockets=None):
        seen["config"] = self.config

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    # Ayrı iş parçacığında: bu modülün eşzamanlı Playwright'ı ana iş parçacığında döngü çalıştırıyor olabilir
    runner = threading.Thread(target=asyncio.run, args=(main.serve(settings, FastAPI()),))
    runner.start()
    runner.join(timeout=10)
    grace = seen["config"].timeout_graceful_shutdown
    assert grace == main.SHUTDOWN_GRACE_SECONDS and 0 < grace <= 30
    assert seen["config"].ws_max_size == main.WS_MAX_SIZE


# Arayüzden bağımsız, ham bir WebSocket oturumu: hello + verilen mesajlar, her birinin ack'i döner.
RAW_SOCKET_JS = """async ([name, messages]) => {
    const ws = new WebSocket(`ws://${location.host}/ws`);
    await new Promise((resolve, reject) => {
        ws.onopen = resolve;
        ws.onerror = () => reject(new Error("WebSocket açılamadı"));
    });
    const waiting = new Map();
    ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === "ack" && waiting.has(msg.rid)) {
            waiting.get(msg.rid)(msg);
            waiting.delete(msg.rid);
        }
    };
    const send = (msg, rid) => new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error(`ack gelmedi: ${msg.type}`)), 5000);
        waiting.set(rid, (ack) => { clearTimeout(timer); resolve(ack); });
        ws.send(JSON.stringify({ ...msg, rid }));
    });
    const acks = [await send({ type: "hello", name }, "hello")];
    for (const [index, msg] of messages.entries()) acks.push(await send(msg, `m${index}`));
    ws.close();
    return acks.map((ack) => ({ ok: ack.ok, message: ack.message }));
}"""
