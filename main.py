# ──────────────────────────────────────────────────────────────
#  main.py — MeetBot giriş noktası
#
#  Kablolama: ayarlar → bot (MeetBot veya FakeBot) → Downloader → Hub
#             → Player → FastAPI uygulaması → uvicorn
#
#  Kullanım:
#     python main.py                  # normal çalışma (Chrome + Google Meet)
#     python main.py --fake-bot       # Chrome/Meet olmadan tam arayüz (geliştirme/demo)
#     python main.py --doctor         # kurulumu denetle
#     python main.py --host 127.0.0.1 --port 8080
# ──────────────────────────────────────────────────────────────

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import io
import logging
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NamedTuple, Optional

import uvicorn
from fastapi import FastAPI

from audio_manager import JS_RUNTIME_MIN_VERSIONS, Downloader, format_version, parse_version
from config import VERSION, Settings, load_settings
from player import Player
from server import WS_MAX_SIZE, Hub, create_app

log = logging.getLogger("meetbot.main")

# WS_MAX_SIZE (server.py, 64 KB): protokol 16 KB'a kadar çerçeveleri kendisi reddeder (bağlantıyı
# kapatmadan); bunun üstündekiler uvicorn tarafından bağlantı düzeyinde kesilir (1009). Küçük
# tutulur: dev çerçeveler olay döngüsünü herkes için meşgul etmesin.

# Bundan kısa, elle yazılmış yönetici şifresi için başlangıçta uyarı
MIN_RECOMMENDED_PASSWORD_LENGTH = 10

# Kapanışta uvicorn açık bağlantıların bitmesini bekler. Windows'ta (Proactor döngüsü) tarayıcı
# bağlantıyı sert kapatınca (WinError 10054) asyncio sunucusunun bağlantı sayacı düşmeyebilir;
# süre sınırı olmazsa Ctrl+C'den sonra süreç "Shutting down"da sonsuza kadar asılı kalır.
SHUTDOWN_GRACE_SECONDS = 10


# ──────────────────────────────────────────────────────────────
#  Komut satırı, konsol ve günlük
# ──────────────────────────────────────────────────────────────

def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port bir sayı olmalı") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port 1–65535 arasında olmalı")
    return port


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="main.py", description=f"MeetBot {VERSION} — Google Meet müzik botu")
    parser.add_argument("--host", help="dinlenecek adres (varsayılan: MEETBOT_HOST, yoksa 0.0.0.0)")
    parser.add_argument("--port", type=_port, help="port (varsayılan: MEETBOT_PORT, yoksa 8000)")
    parser.add_argument("--fake-bot", action="store_true",
                        help="Chrome/Meet olmadan sahte botla çalış (gerçek indirici, tam arayüz)")
    parser.add_argument("--doctor", action="store_true", help="kurulumu denetle ve çık")
    return parser.parse_args(argv)


def configure_console() -> None:
    """Emoji'li günlükler cp1254 konsolda / yönlendirilmiş çıktıda çökmesin."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


def install_break_handler() -> None:
    """Windows: Ctrl+Break da Ctrl+C gibi temiz kapansın.

    uvicorn kapanışı bitirince yakaladığı sinyali yeniden yükseltir; SIGBREAK'in varsayılan
    işleyicisi süreci 3 çıkış koduyla öldürür. KeyboardInterrupt'a çevirince main() bitirir.
    """
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def lan_ip() -> Optional[str]:
    """Aynı ağdaki cihazların kullanacağı yerel IP (UDP 'connect' paket göndermez)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
        except OSError as exc:
            log.debug("Yerel ağ adresi bulunamadı: %s", exc)
            return None


def url_host(host: str) -> str:
    """Tarayıcıda açılacak adres: joker/loopback → localhost, IPv6 → [adres]."""
    if host in ("0.0.0.0", "::", "127.0.0.1", "::1"):
        return "localhost"
    return f"[{host}]" if ":" in host else host


def print_banner(settings: Settings, fake_bot: bool) -> None:
    line = "═" * 60
    print(line)
    print(f"  🎵  MeetBot {VERSION} — Google Meet Müzik Botu")
    print(line)
    print(f"  🌐  Bu bilgisayar : http://{url_host(settings.host)}:{settings.port}")
    if settings.host == "0.0.0.0":  # yalnızca IPv4 jokerinde yerel ağ IPv4 adresi geçerli
        ip = lan_ip()
        if ip:
            print(f"  📶  Aynı ağdakiler: http://{ip}:{settings.port}")
    if settings.admin_password_generated:
        print(f"  🔐  Yönetici şifresi: {settings.admin_password}")
        print("      (kalıcı yapmak için .env dosyasına MEETBOT_ADMIN_PASSWORD=... yazın)")
    elif len(settings.admin_password) < MIN_RECOMMENDED_PASSWORD_LENGTH:
        print(f"  ⚠️  Yönetici şifresi kısa ({len(settings.admin_password)} karakter): aynı ağdakiler "
              f"denemeyle bulabilir. En az {MIN_RECOMMENDED_PASSWORD_LENGTH} karakterlik, tahmin edilmesi "
              "zor bir MEETBOT_ADMIN_PASSWORD kullanın.")
    if fake_bot:
        print("  🧪  Sahte bot modu: Chrome ve Google Meet kullanılmıyor")
    print("  📋  Meet bağlantısını web arayüzünden (yönetici olarak) girin.")
    print(line, flush=True)


# ──────────────────────────────────────────────────────────────
#  Kablolama
# ──────────────────────────────────────────────────────────────

class Application(NamedTuple):
    app: FastAPI
    player: Player
    bot: Any
    downloader: Downloader


def build_bot(settings: Settings, fake_bot: bool) -> Any:
    if fake_bot:
        from fake_bot import FakeBot
        return FakeBot(settings)
    from bot import MeetBot  # Chrome/Playwright yalnızca gerçek modda gerekir
    return MeetBot(settings)


def build_application(settings: Settings, fake_bot: bool = False) -> Application:
    bot = build_bot(settings, fake_bot)
    downloader = Downloader(settings)
    hub = Hub()
    player = Player(settings, bot, downloader, hub.broadcast)
    bot.on_status = player.on_bot_status
    bot.on_track_ended = player.on_track_ended
    bot.on_progress = player.on_progress
    # Kapanışta create_app'in lifespan'i önce player.shutdown() (indirmeler), sonra bot.shutdown() çağırır
    app = create_app(settings, player, bot, hub)
    return Application(app, player, bot, downloader)


def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Windows ProactorLoop: istemci bağlantıyı sert kapattığında (WinError 10054) gürültü yapma."""
    exc = context.get("exception")
    if isinstance(exc, ConnectionResetError) or "WinError 10054" in str(context.get("message", "")):
        log.debug("İstemci bağlantıyı kapattı: %s", exc or context.get("message"))
        return
    loop.default_exception_handler(context)


async def _app_shutdown(app: FastAPI) -> None:
    """create_app'in tek seferlik kapanışı (oynatıcı → bot); zaten çalıştıysa hemen döner."""
    shutdown = getattr(app.state, "shutdown", None)
    if shutdown is not None:
        await shutdown()


class MeetBotServer(uvicorn.Server):
    """uvicorn.Server + her durumda uygulama kapanışı.

    uvicorn, açık bağlantıların kapanmasını beklerken gelen ikinci Ctrl+C'de (force_exit)
    lifespan kapanışını HİÇ çalıştırmaz; oynatıcı ve bot kapanmaz, Chrome toplantıda kalırdı.
    Burada uygulama kapanışı sunucu kapanışının sonunda yine de çalışır (lifespan zaten
    çalıştırdıysa bir şey yapmaz). Bu sırada gelen Ctrl+C'ler uvicorn'da yalnızca bayrak
    kaldırır; kapanışı yarıda kesmez.
    """

    def __init__(self, config: uvicorn.Config, app: FastAPI):
        super().__init__(config)
        self._app = app

    async def shutdown(self, sockets: Optional[list[socket.socket]] = None) -> None:
        try:
            await super().shutdown(sockets=sockets)
        finally:
            if self.force_exit:
                log.warning("⚠️  Zorla kapatılıyor; oynatıcı ve bot yine de kapatılıyor (Chrome toplantıda kalmasın)...")
            await _app_shutdown(self._app)


def make_server(settings: Settings, app: FastAPI) -> MeetBotServer:
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,           # kendi logging ayarımızı kullan
        log_level=settings.log_level.lower(),
        access_log=False,
        ws_max_size=WS_MAX_SIZE,
        timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS,
    )
    return MeetBotServer(config, app)


async def serve(settings: Settings, app: FastAPI) -> None:
    asyncio.get_running_loop().set_exception_handler(_loop_exception_handler)
    try:
        await make_server(settings, app).serve()
    finally:
        # Son güvence (ör. başlangıç hatası): kapanış zaten çalıştıysa beklemeden döner
        await _app_shutdown(app)


# ──────────────────────────────────────────────────────────────
#  --doctor
# ──────────────────────────────────────────────────────────────

def _command_version(command: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"çalıştırılamadı: {exc}"
    lines = (result.stdout or result.stderr).strip().splitlines()
    first = lines[0] if lines else f"çıkış kodu {result.returncode}"
    return result.returncode == 0, first


def _check_writable(directory: Path) -> tuple[bool, str]:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".doctor-"):
            pass
    except OSError as exc:
        return False, f"{directory} yazılamıyor: {exc}"
    return True, str(directory)


def _check_port(host: str, port: int) -> tuple[bool, str]:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError as exc:
            return False, f"{host}:{port} kullanılamıyor ({exc.strerror or exc}) — --port ile değiştirin"
    return True, f"{host}:{port} boş"


_JS_RUNTIME_NAMES = {"node": "Node.js", "deno": "Deno", "bun": "Bun"}


def _check_js_runtime(mode: str) -> Optional[tuple[bool, str, str]]:
    """yt-dlp'nin YouTube JS doğrulaması için kullanacağı çalışma zamanı (ayara göre)."""
    if mode == "none":
        return None  # kullanıcı bilerek kapattı
    executable = "node" if mode == "auto" else mode
    label = _JS_RUNTIME_NAMES.get(executable, executable)
    path = shutil.which(executable)
    if path is None:
        hint = "https://nodejs.org" if executable == "node" else "MEETBOT_YTDLP_JS_RUNTIME ayarını kontrol edin"
        return False, label, f"bulunamadı — {hint} (YouTube JS doğrulaması için)"
    ok, detail = _command_version([path, "--version"])
    if not ok:
        return False, label, f"{detail} ({path})"
    # yt-dlp eski sürümleri sessizce yok sayar (Node < 22): "var" olması yetmez
    minimum = JS_RUNTIME_MIN_VERSIONS.get(executable)
    if minimum is not None:
        version = parse_version(detail)
        if version is None or version < minimum:
            return False, label, (f"{detail} — yt-dlp en az {format_version(minimum)} istiyor, "
                                  f"güncelleyin ({path})")
    return True, label, f"{detail} ({path})"


def run_doctor(settings: Settings) -> int:
    from bot import find_chrome, needs_virtual_display

    checks: list[tuple[bool, str, str]] = []

    chrome = find_chrome(settings.chrome_path)
    checks.append((chrome is not None, "Chrome / Edge",
                   chrome or "bulunamadı — Google Chrome kurun veya MEETBOT_CHROME_PATH ayarlayın"))

    if needs_virtual_display():
        # Ekransız Linux sunucu: Chrome'u sanal ekranda (Xvfb) açacağız
        xvfb = shutil.which("Xvfb")
        checks.append((xvfb is not None, "Sanal ekran",
                       f"Xvfb ({xvfb})" if xvfb else "Xvfb yok — sudo apt install xvfb"))

    ok, detail = _command_version([sys.executable, "-m", "yt_dlp", "--version"])
    checks.append((ok, "yt-dlp", detail if ok else f"{detail} — pip install -r requirements.txt"))

    ejs = importlib.util.find_spec("yt_dlp_ejs") is not None
    checks.append((ejs, "yt-dlp-ejs", "kurulu" if ejs else "yok — pip install -r requirements.txt"))

    js_check = _check_js_runtime(settings.ytdlp_js_runtime)
    if js_check is not None:
        checks.append(js_check)

    writable, writable_detail = _check_writable(settings.downloads_dir)
    checks.append((writable, "İndirme klasörü", writable_detail))
    port_ok, port_detail = _check_port(settings.host, settings.port)
    checks.append((port_ok, "Port", port_detail))

    print(f"🩺  MeetBot {VERSION} kurulum denetimi\n")
    for ok, name, detail in checks:
        print(f"  {'✅' if ok else '❌'}  {name:<16} {detail}")
    healthy = all(ok for ok, _, _ in checks)
    print("\n  🎉  Her şey hazır!" if healthy else "\n  ⚠️  Eksikleri giderip tekrar deneyin.")
    return 0 if healthy else 1


# ──────────────────────────────────────────────────────────────
#  Ana fonksiyon
# ──────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    configure_console()
    args = parse_args(argv)
    settings = load_settings()
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    setup_logging(settings.log_level)

    if args.doctor:
        return run_doctor(settings)

    application = build_application(settings, fake_bot=args.fake_bot)
    application.downloader.purge_all()
    print_banner(settings, args.fake_bot)
    install_break_handler()
    try:
        asyncio.run(serve(settings, application.app))
    except KeyboardInterrupt:
        pass  # Ctrl+C / Ctrl+Break: uvicorn kapanışı (lifespan → oynatıcı + bot) zaten tamamladı
    print("👋  MeetBot kapandı.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
