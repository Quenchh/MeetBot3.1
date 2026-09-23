# ──────────────────────────────────────────────────────────────
#  server.py — FastAPI uygulaması + WebSocket protokolü v2
#
#  • /ws        : JSON mesajlar (hello → welcome el sıkışması, ack/rid, yetkiler)
#  • /          : index.html (önbelleksiz)
#  • /static/*  : arayüz dosyaları
#  • /api/health: durum özeti
#
#  İndirilen ses dosyaları HTTP üzerinden SUNULMAZ (bot dosyayı diskten okuyup
#  sayfaya yükler), bu yüzden CORS'a da gerek yoktur.
# ──────────────────────────────────────────────────────────────

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import math
import re
import secrets
import time
import unicodedata
from collections import Counter, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Coroutine, Iterable, Mapping, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers

from bot import VIEW_KEYS, VIEW_MAX_TEXT, VIEW_TARGETS, BotError
from config import BASE_DIR, VERSION, Settings
from player import MAX_QUERY_LENGTH, Player, PlayerError

log = logging.getLogger("meetbot.server")

STATIC_DIR = BASE_DIR / "static"

# ──────────────────────────────────────────────────────────────
#  Protokol sınırları
# ──────────────────────────────────────────────────────────────

MAX_FRAME_BYTES = 16 * 1024
WS_MAX_SIZE = 4 * MAX_FRAME_BYTES  # uvicorn ws_max_size: bunun üstündeki çerçeve bağlantıyı 1009 ile kapatır
RATE_LIMIT = 30              # mesaj
RATE_WINDOW = 10.0           # saniye
FLOOD_LIMIT = 2 * RATE_LIMIT  # RATE_WINDOW içinde (reddedilenler dahil) bundan fazla çerçeve → bağlantı 1008 ile kapanır
MAX_AUTH_FAILURES = 5        # adres başına art arda hatalı şifre → kilit
AUTH_LOCKOUT = 60.0          # saniye; aynı adres yeniden kilitlendikçe ikiye katlanır...
AUTH_LOCKOUT_MAX = 3600.0    # ...en fazla bu kadar (sessiz geçen bu süreden sonra adres unutulur)
AUTH_GLOBAL_LIMIT = 30       # tüm adreslerden AUTH_GLOBAL_WINDOW içinde bu kadar hatalı şifre →
AUTH_GLOBAL_WINDOW = 60.0    # pencere boşalana kadar kimse şifre deneyemez (çok adresli tahmine karşı)
SEND_TIMEOUT = 2.0           # istemci başına gönderim zaman aşımı
MAX_CONNECTIONS = 100
MAX_CONNECTIONS_PER_IP = 10  # tek bir adres tüm sınırı dolduramasın (döngü adresi / yerel vekil hariç)
HELLO_TIMEOUT = 10.0         # saniye; bu sürede hello ile oturum açmayan bağlantı kapatılır
MAX_INBOX = 20               # bağlantı başına işlenmeyi bekleyen en fazla komut
MAX_PENDING_ADDS = 3         # bağlantı başına aynı anda çözümlenen ekleme isteği
MAX_NAME_LENGTH = 32
MAX_RID_LENGTH = 64
MAX_PASSWORD_LENGTH = 256
MAX_LINK_LENGTH = 2048

PUBLIC_TYPES = ("ping", "state", "auth", "logout", "add", "remove")
CONTROL_TYPES = ("move", "play_now", "shuffle", "pause", "resume", "skip", "seek", "repeat", "volume")
ADMIN_TYPES = ("clear", "stop", "mic", "join_meet", "leave_meet")
# Bot ekranı: yönetici + (varsayılan) yalnızca bu bilgisayardan / SSH tüneliyle (MEETBOT_REMOTE_VIEW)
VIEW_TYPES = ("view_start", "view_stop", "view_input", "google_logout")
VIEW_ACTIONS = ("click", "type", "key", "scroll", "back", "reload")
VIEW_INTERVAL = 0.5          # sn — izleyici varken ekran görüntüsü aralığı
VIEW_ERROR_RETRY = 1.0       # sn — ekran görüntüsü alınamazsa bekleme
VIEW_MAX_FAILURES = 5        # art arda bu kadar hata → bot ekranı kapatılır

MEET_LINK_RE = re.compile(
    r"https://meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3}|lookup/[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_BIDI_CONTROLS = {chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))}
# Host başlığı: ad[:port] ya da [IPv6][:port]
_HOST_HEADER_RE = re.compile(r"^(?:\[(?P<ipv6>[0-9A-Za-z:.%]+)\]|(?P<name>[A-Za-z0-9._-]+))(?::\d{1,5})?$")
# İnternette kimsenin alamayacağı, yalnızca yerel ağda çözülen ad uzantıları
_LOCAL_NAME_SUFFIXES = (".localhost", ".local", ".lan", ".home", ".internal", ".intranet", ".home.arpa",
                        ".localdomain")


class ProtocolError(Exception):
    """İstemciye gönderilecek Türkçe hata mesajı."""


def permissions_for(is_admin: bool, guest_controls: bool, remote_view: bool = False) -> list[str]:
    allowed = list(PUBLIC_TYPES)
    if is_admin or guest_controls:
        allowed += CONTROL_TYPES
    if is_admin:
        allowed += ADMIN_TYPES
        if remote_view:
            allowed += VIEW_TYPES
    return allowed


def parse_meet_link(text: str) -> Optional[str]:
    """Metnin içindeki ilk geçerli Meet bağlantısını kanonik hale getirir; sonrasını atar."""
    match = MEET_LINK_RE.search(text)
    if not match:
        return None
    code = match.group(1)
    if code.lower().startswith("lookup/"):
        code = "lookup/" + code[len("lookup/"):]
    else:
        code = code.lower()
    return f"https://meet.google.com/{code}"


def clean_name(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = "".join(
        ch for ch in value
        if unicodedata.category(ch) != "Cc" and ch not in _BIDI_CONTROLS
    )
    text = " ".join(text.split())
    return text if 1 <= len(text) <= MAX_NAME_LENGTH else None


def _normalize_netloc(netloc: str, scheme: str) -> str:
    netloc = netloc.strip().lower()
    default_port = ":443" if scheme in ("https", "wss") else ":80"
    return netloc[: -len(default_port)] if netloc.endswith(default_port) else netloc


def origin_allowed(headers: Mapping[str, str]) -> bool:
    """Origin başlığı varsa, alan adı:port'u istek Host başlığıyla aynı olmalı (CSWSH koruması)."""
    origin = headers.get("origin")
    if origin is None:
        return True
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    host = headers.get("host", "")
    return _normalize_netloc(parts.netloc, parts.scheme) == _normalize_netloc(host, parts.scheme)


def normalize_hostnames(names: Iterable[str]) -> tuple[str, ...]:
    return tuple(name.strip().lower().rstrip(".") for name in names if name and name.strip())


def host_header_allowed(host: Optional[str], trusted: tuple[str, ...] = ()) -> bool:
    """DNS rebinding koruması: Host başlığı sunucunun gerçekten kullanılan bir adı olmalı.

    Kabul edilenler: IP adresleri, tek parçalı adlar (localhost, bilgisayar adı), yalnızca yerel
    ağda çözülen uzantılar (.local, .lan, .home.arpa...) ve `trusted` listesi
    (MEETBOT_TRUSTED_HOSTNAMES; "*" denetimi kapatır). Saldırganın DNS'ini yönettiği internet alan
    adları (evil.example) reddedilir: rebinding sonrası Origin ile Host aynı olsa bile.
    """
    if not host:
        return True  # Host göndermeyen istemci tarayıcı değildir (rebinding tarayıcı gerektirir)
    if "*" in trusted:
        return True
    match = _HOST_HEADER_RE.match(host.strip())
    if not match:
        return False
    if match.group("ipv6") is not None:
        try:
            ipaddress.ip_address(match.group("ipv6").split("%", 1)[0])
        except ValueError:
            return False
        return True
    name = match.group("name").lower().rstrip(".")
    if not name:
        return False
    if name in trusted or "." not in name or name.endswith(_LOCAL_NAME_SUFFIXES):
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def is_loopback(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return ip.is_loopback or (mapped is not None and mapped.is_loopback)


# ── Mesaj alanı doğrulayıcıları ───────────────────────────────

def _int_field(msg: dict, key: str) -> int:
    value = msg.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"'{key}' bir tam sayı olmalı")
    return value


def _number_field(msg: dict, key: str) -> float:
    value = msg.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProtocolError(f"'{key}' bir sayı olmalı")
    return float(value)


def _str_field(msg: dict, key: str, max_length: int) -> str:
    value = msg.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ProtocolError(f"'{key}' 1–{max_length} karakterlik bir metin olmalı")
    return value.strip()


def _bool_field(msg: dict, key: str) -> bool:
    value = msg.get(key)
    if not isinstance(value, bool):
        raise ProtocolError(f"'{key}' true/false olmalı")
    return value


# ──────────────────────────────────────────────────────────────
#  Bağlantılar ve yayın
# ──────────────────────────────────────────────────────────────

Command = tuple[str, dict, Optional[str]]  # (type, mesaj, rid)


class Client:
    """Tek bir WebSocket oturumu."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.id = secrets.token_hex(4)
        self.name = ""
        self.is_admin = False
        self.token: Optional[str] = None
        self.ready = False            # hello tamamlandı mı
        self.closed = False
        self.send_lock = asyncio.Lock()
        self.pending_adds = 0
        # Komutlar sırayla işlenir; None → işçi dursun (bağlantı kapandı)
        self.inbox: asyncio.Queue[Optional[Command]] = asyncio.Queue()
        self._recent: deque[float] = deque()   # hız sınırına sayılan çerçeveler
        self._frames: deque[float] = deque()   # reddedilenler dahil TÜM çerçeveler (sel denetimi)

    @property
    def address(self) -> str:
        client = getattr(self.ws, "client", None)
        return client.host if client is not None and client.host else "?"

    def allow_message(self, now: float) -> bool:
        while self._recent and now - self._recent[0] >= RATE_WINDOW:
            self._recent.popleft()
        if len(self._recent) >= RATE_LIMIT:
            return False
        self._recent.append(now)
        return True

    def flooding(self, now: float) -> bool:
        """Hız sınırına rağmen durmadan gönderiyor mu? (True → bağlantı kapatılmalı)"""
        self._frames.append(now)
        while self._frames and now - self._frames[0] >= RATE_WINDOW:
            self._frames.popleft()
        return len(self._frames) > FLOOD_LIMIT

    async def _send_locked(self, data: str) -> None:
        async with self.send_lock:
            await self.ws.send_text(data)

    async def send(self, data: str, timeout: float) -> bool:
        if self.closed:
            return False
        try:
            await asyncio.wait_for(self._send_locked(data), timeout)
            return True
        except Exception as exc:
            log.info("🔌  %s istemcisine gönderilemedi: %s", self.name or self.id, type(exc).__name__)
            return False


class Hub:
    """Bağlantı kaydı + eşzamanlı, zaman aşımlı yayın (yavaş/ölü istemciler düşürülür)."""

    def __init__(self, send_timeout: float = SEND_TIMEOUT):
        self.send_timeout = send_timeout
        self._clients: dict[str, Client] = {}

    def __len__(self) -> int:
        return len(self._clients)

    @property
    def clients(self) -> list[Client]:
        return list(self._clients.values())

    def listeners(self) -> list[str]:
        return sorted({c.name for c in self._clients.values()}, key=str.casefold)

    def listeners_msg(self) -> dict:
        names = self.listeners()
        return {"type": "listeners", "listeners": names, "count": len(names)}

    def register(self, client: Client) -> None:
        self._clients[client.id] = client

    def unregister(self, client: Client) -> bool:
        return self._clients.pop(client.id, None) is not None

    async def send(self, client: Client, message: dict) -> bool:
        ok = await client.send(json.dumps(message, ensure_ascii=False), self.send_timeout)
        if not ok:
            await self.drop([client])
        return ok

    async def broadcast(self, message: dict) -> None:
        clients = list(self._clients.values())  # gönderim sırasında liste değişebilir
        if not clients:
            return
        data = json.dumps(message, ensure_ascii=False)
        results = await asyncio.gather(*(c.send(data, self.send_timeout) for c in clients))
        dead = [c for c, ok in zip(clients, results) if not ok]
        if dead:
            await self.drop(dead)

    async def drop(self, clients: list[Client]) -> None:
        removed = [c for c in clients if self.unregister(c)]
        for client in clients:
            client.closed = True
        await asyncio.gather(*(self._close(c) for c in clients))
        if removed:
            log.warning("✂️  Yanıt vermeyen %d istemci düşürüldü", len(removed))
            await self.broadcast(self.listeners_msg())

    @staticmethod
    async def _close(client: Client) -> None:
        try:
            await asyncio.wait_for(client.ws.close(code=1011), timeout=1.0)
        except Exception as exc:
            log.debug("Bağlantı kapatılamadı (%s): %s", client.id, type(exc).__name__)


# ──────────────────────────────────────────────────────────────
#  Yönetici girişi: hatalı deneme sınırı
# ──────────────────────────────────────────────────────────────

@dataclass
class _AuthRecord:
    failures: int = 0          # art arda hatalı deneme (kilitten sonra sıfırlanır)
    lockouts: int = 0          # bu adres kaç kez kilitlendi (süre her seferinde ikiye katlanır)
    locked_until: float = 0.0
    last_seen: float = 0.0


class AuthLimiter:
    """Hatalı yönetici şifresi denemelerini ADRES başına ve toplamda sınırlar.

    • Bir adresten art arda MAX_AUTH_FAILURES hatalı deneme → o adres AUTH_LOCKOUT sn kilitlenir;
      aynı adres yeniden kilitlendikçe süre ikiye katlanır (en fazla AUTH_LOCKOUT_MAX). Sayaç
      bağlantıya değil adrese bağlıdır: yeniden bağlanmak ya da paralel bağlantılar onu sıfırlamaz.
    • Tüm adreslerden AUTH_GLOBAL_WINDOW içinde AUTH_GLOBAL_LIMIT hatalı deneme → pencere
      boşalana kadar kimse şifre deneyemez (çok adresli tahmin saldırısına karşı).
    • Başarılı giriş o adresin geçmişini siler.
    """

    def __init__(self) -> None:
        self._by_address: dict[str, _AuthRecord] = {}
        self._recent: deque[float] = deque()

    def _forget_stale(self, now: float) -> None:
        while self._recent and now - self._recent[0] >= AUTH_GLOBAL_WINDOW:
            self._recent.popleft()
        stale = [address for address, record in self._by_address.items()
                 if now - max(record.last_seen, record.locked_until) >= AUTH_LOCKOUT_MAX]
        for address in stale:
            del self._by_address[address]

    def wait_time(self, address: str, now: float) -> float:
        """Bu adres şifre denemeden önce kaç sn beklemeli? (0 → deneyebilir)"""
        self._forget_stale(now)
        record = self._by_address.get(address)
        wait = record.locked_until - now if record is not None else 0.0
        if len(self._recent) >= AUTH_GLOBAL_LIMIT:
            wait = max(wait, self._recent[0] + AUTH_GLOBAL_WINDOW - now)
        return max(0.0, wait)

    def failure(self, address: str, now: float) -> tuple[int, float]:
        """Hatalı denemeyi kaydeder → (art arda hata sayısı, yeni kilit süresi ya da 0)."""
        record = self._by_address.setdefault(address, _AuthRecord())
        record.failures += 1
        record.last_seen = now
        self._recent.append(now)
        if record.failures < MAX_AUTH_FAILURES:
            return record.failures, 0.0
        failures, record.failures = record.failures, 0
        duration = min(AUTH_LOCKOUT * 2 ** min(record.lockouts, 16), AUTH_LOCKOUT_MAX)
        record.lockouts += 1
        record.locked_until = now + duration
        return failures, duration

    def success(self, address: str) -> None:
        self._by_address.pop(address, None)


# ──────────────────────────────────────────────────────────────
#  Uygulama
# ──────────────────────────────────────────────────────────────

@dataclass
class Reply:
    message: Optional[str] = None
    data: Any = None


Handler = Callable[[Client, dict], Awaitable[Optional[Reply]]]


class _NoCacheStaticFiles(StaticFiles):
    def file_response(self, *args: Any, **kwargs: Any):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class _TrustedHostMiddleware:
    """HTTP isteklerinde Host başlığını denetler (DNS rebinding ile arayüz/durum okunmasın).
    WebSocket el sıkışması uç noktada ayrıca denetlenir (1008 ile kapatılır)."""

    def __init__(self, app: Any, trusted: tuple[str, ...]):
        self.app = app
        self.trusted = trusted

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            host = Headers(scope=scope).get("host")
            if not host_header_allowed(host, self.trusted):
                log.warning("🚫  Tanınmayan Host başlığı reddedildi: %s", (host or "")[:100])
                response = PlainTextResponse(
                    "Geçersiz Host başlığı (alan adıyla erişiyorsanız MEETBOT_TRUSTED_HOSTNAMES ayarına ekleyin)",
                    status_code=400,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(settings: Settings, player: Player, bot: Any, hub: Hub) -> FastAPI:
    admin_tokens: set[str] = set()
    background: set[asyncio.Task] = set()
    open_sockets = 0
    sockets_by_address: Counter[str] = Counter()
    auth_limiter = AuthLimiter()
    trusted_hosts = normalize_hostnames((*settings.trusted_hostnames, settings.host))
    shutdown_task: Optional[asyncio.Task] = None
    viewers: dict[str, Client] = {}              # bot ekranını izleyen yönetici oturumları
    view_task: Optional[asyncio.Task] = None
    view_last: dict[str, Any] = {"key": None}   # son gönderilen kare (değişmeyen kare tekrar gönderilmez)

    async def run_shutdown() -> None:
        log.info("🛑  Sunucu kapanıyor...")
        for task in list(background):
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        # Önce oynatıcı (indirmeler / yt-dlp süreçleri), sonra bot (Chrome); biri patlasa da diğeri çalışsın
        for name, shutdown in (("oynatıcı", player.shutdown), ("bot", bot.shutdown)):
            try:
                await shutdown()
            except Exception:
                log.exception("❌  Kapanışta %s durdurulamadı", name)

    async def shutdown_once() -> None:
        """Uygulama kapanışı, TEK sefer: lifespan'den ve (uvicorn ikinci Ctrl+C'de lifespan'i
        atlayabildiği için) main.py'nin sunucu kapanışından çağrılır. Sonraki çağrılar ilkinin
        bitmesini bekler; bittiyse hemen döner."""
        nonlocal shutdown_task
        if shutdown_task is None:
            shutdown_task = asyncio.get_running_loop().create_task(run_shutdown(), name="meetbot-shutdown")
        await asyncio.shield(shutdown_task)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await shutdown_once()

    app = FastAPI(title="MeetBot", version=VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.shutdown = shutdown_once
    app.add_middleware(_TrustedHostMiddleware, trusted=trusted_hosts)

    # ── HTTP ──────────────────────────────────────────────────

    @app.get("/", include_in_schema=False)
    async def index():
        index_file = STATIC_DIR / "index.html"
        if not index_file.is_file():
            return JSONResponse({"detail": "index.html bulunamadı"}, status_code=404)
        return FileResponse(index_file, headers={"Cache-Control": "no-cache"})

    @app.get("/api/health")
    async def health():
        return {
            "ok": True,
            "version": VERSION,
            "bot_status": bot.status,
            "queue_length": len(player.queue),
            "listeners": len(hub.listeners()),
        }

    if STATIC_DIR.is_dir():
        app.mount("/static", _NoCacheStaticFiles(directory=STATIC_DIR), name="static")

    # ── Yardımcılar ───────────────────────────────────────────

    def snapshot() -> dict:
        return {**player.snapshot(), "listeners": hub.listeners()}

    def view_allowed(client: Client) -> bool:
        """Bot ekranı bu bağlantıdan açılabilir mi? ("local": yalnızca döngü adresi = sunucu / SSH tüneli)"""
        if settings.remote_view == "on":
            return True
        return settings.remote_view == "local" and is_loopback(client.address)

    def permissions(client: Client) -> list[str]:
        return permissions_for(client.is_admin, settings.guest_controls, view_allowed(client))

    def session_msg(client: Client) -> dict:
        return {"type": "session", "is_admin": client.is_admin, "permissions": permissions(client)}

    async def reply_ok(client: Client, rid: Optional[str], reply: Optional[Reply]) -> None:
        if rid is None:
            return
        reply = reply or Reply()
        await hub.send(client, {"type": "ack", "rid": rid, "ok": True,
                                "message": reply.message, "data": reply.data})

    async def reply_error(client: Client, rid: Optional[str], message: str) -> None:
        if rid is not None:
            await hub.send(client, {"type": "ack", "rid": rid, "ok": False, "message": message, "data": None})
        else:
            await hub.send(client, {"type": "error", "message": message})

    def spawn(coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task:
        """Arka plan görevi: kapanışta (lifespan) iptal edilir."""
        task = asyncio.get_running_loop().create_task(coro, name=name)
        background.add(task)
        task.add_done_callback(background.discard)
        return task

    # ── Mesaj işleyicileri ────────────────────────────────────

    async def on_hello(client: Client, msg: dict) -> Optional[Reply]:
        if client.closed:
            return None  # hello sıradayken bağlantı koptu: dinleyici olarak kaydetme
        if client.ready:
            raise ProtocolError("Oturum zaten başlatıldı")
        name = clean_name(msg.get("name"))
        if name is None:
            raise ProtocolError(f"İsim 1–{MAX_NAME_LENGTH} karakter olmalı")
        token = msg.get("token")
        client.name = name
        if isinstance(token, str) and token in admin_tokens:
            client.is_admin, client.token = True, token
        async with client.send_lock:
            # Kayıt + welcome kilit altında: araya giren yayınlar welcome'dan SONRA gelir
            client.ready = True
            hub.register(client)
            welcome = {
                "type": "welcome",
                "client_id": client.id,
                "name": client.name,
                "is_admin": client.is_admin,
                "permissions": permissions(client),
                "version": VERSION,
                "limits": settings.public_config,
                "state": snapshot(),
            }
            try:
                await asyncio.wait_for(client.ws.send_text(json.dumps(welcome, ensure_ascii=False)),
                                       hub.send_timeout)
                sent = True
            except Exception as exc:
                log.info("🔌  %s istemcisine welcome gönderilemedi: %s", client.name, type(exc).__name__)
                sent = False
        if not sent:
            await hub.drop([client])
            return None
        log.info("👋  %s bağlandı%s", client.name, " (yönetici)" if client.is_admin else "")
        await hub.broadcast(hub.listeners_msg())
        return None

    async def on_ping(client: Client, msg: dict) -> Optional[Reply]:
        await hub.send(client, {"type": "pong"})
        return None

    async def on_state(client: Client, msg: dict) -> Optional[Reply]:
        await hub.send(client, {"type": "state", **snapshot()})
        return None

    async def on_auth(client: Client, msg: dict) -> Optional[Reply]:
        # Kilit ADRES başınadır: yeni bağlantı açmak ya da paralel bağlantılar sayacı sıfırlamaz
        address = client.address
        now = time.monotonic()
        wait = auth_limiter.wait_time(address, now)
        if wait > 0:
            raise ProtocolError(f"Çok fazla hatalı deneme, {math.ceil(wait)} sn sonra tekrar dene")
        password = msg.get("password")
        valid = (
            isinstance(password, str)
            and len(password) <= MAX_PASSWORD_LENGTH
            and hmac.compare_digest(password.encode("utf-8"), settings.admin_password.encode("utf-8"))
        )
        if not valid:
            failures, lockout = auth_limiter.failure(address, now)
            log.warning("🔒  Hatalı yönetici şifresi (%s, %s, %d. deneme)", client.name, address, failures)
            if lockout:
                log.warning("🔒  %s adresi %d sn boyunca şifre deneyemeyecek", address, math.ceil(lockout))
                raise ProtocolError(f"Çok fazla hatalı deneme, {math.ceil(lockout)} sn sonra tekrar dene")
            raise ProtocolError("Şifre yanlış")
        auth_limiter.success(address)
        token = secrets.token_urlsafe(24)
        admin_tokens.add(token)
        client.is_admin, client.token = True, token
        log.info("🔐  %s yönetici olarak giriş yaptı", client.name)
        await hub.send(client, session_msg(client))
        return Reply("Yönetici girişi başarılı", {"token": token})

    async def on_logout(client: Client, msg: dict) -> Optional[Reply]:
        token = client.token
        client.is_admin, client.token = False, None
        await drop_viewer(client)
        if token is not None:
            admin_tokens.discard(token)
            # Aynı jetonla açılmış diğer sekmeler de yetkisini kaybeder
            for other in hub.clients:
                if other is not client and other.token == token:
                    other.is_admin, other.token = False, None
                    await drop_viewer(other)
                    await hub.send(other, session_msg(other))
        await hub.send(client, session_msg(client))
        return Reply("Çıkış yapıldı")

    async def run_add(client: Client, rid: Optional[str], query: str) -> None:
        try:
            tracks = await player.add(query, client.name)
        except PlayerError as exc:
            await reply_error(client, rid, str(exc))
        except Exception:
            log.exception("❌  Ekleme işlenirken beklenmeyen hata")
            await reply_error(client, rid, "Beklenmeyen bir hata oluştu")
        else:
            what = tracks[0].title if len(tracks) == 1 else f"{len(tracks)} şarkı"
            await reply_ok(client, rid, Reply(f"🎵 {what} kuyruğa eklendi", {"tracks": [t.public() for t in tracks]}))
        finally:
            client.pending_adds -= 1

    async def on_add(client: Client, msg: dict) -> Optional[Reply]:
        query = _str_field(msg, "query", MAX_QUERY_LENGTH)
        if client.pending_adds >= MAX_PENDING_ADDS:
            raise ProtocolError("Önceki eklemelerin bitmesini bekle")
        client.pending_adds += 1
        # Çözümleme uzun sürebilir (oynatma listesi) → okuma döngüsünü bloklamadan arka planda
        spawn(run_add(client, msg.get("rid"), query), f"add:{client.id}")
        return None

    async def on_remove(client: Client, msg: dict) -> Optional[Reply]:
        track = await player.remove(
            _int_field(msg, "id"),
            requested_by=client.name,
            force=client.is_admin or settings.guest_controls,
        )
        return Reply(f"{track.title} kuyruktan çıkarıldı")

    async def on_move(client: Client, msg: dict) -> Optional[Reply]:
        await player.move(_int_field(msg, "id"), _int_field(msg, "index"))
        return None

    async def on_play_now(client: Client, msg: dict) -> Optional[Reply]:
        await player.play_now(_int_field(msg, "id"))
        return None

    async def on_shuffle(client: Client, msg: dict) -> Optional[Reply]:
        await player.shuffle()
        return Reply("🔀 Kuyruk karıştırıldı")

    async def on_clear(client: Client, msg: dict) -> Optional[Reply]:
        await player.clear()
        return Reply("🧹 Kuyruk temizlendi")

    async def on_pause(client: Client, msg: dict) -> Optional[Reply]:
        await player.pause()
        return None

    async def on_resume(client: Client, msg: dict) -> Optional[Reply]:
        await player.resume()
        return None

    async def on_skip(client: Client, msg: dict) -> Optional[Reply]:
        await player.skip()
        return None

    async def on_stop(client: Client, msg: dict) -> Optional[Reply]:
        await player.stop()
        return None

    async def on_seek(client: Client, msg: dict) -> Optional[Reply]:
        position = _number_field(msg, "position")
        if position < 0:
            raise ProtocolError("'position' negatif olamaz")
        await player.seek(position)
        return None

    async def on_repeat(client: Client, msg: dict) -> Optional[Reply]:
        await player.set_repeat(_str_field(msg, "mode", 8))
        return None

    async def on_volume(client: Client, msg: dict) -> Optional[Reply]:
        await player.set_volume(_str_field(msg, "target", 8), _int_field(msg, "value"))
        return None

    async def on_mic(client: Client, msg: dict) -> Optional[Reply]:
        await player.set_mic_muted(_bool_field(msg, "muted"))
        return None

    async def on_join_meet(client: Client, msg: dict) -> Optional[Reply]:
        link = parse_meet_link(_str_field(msg, "link", MAX_LINK_LENGTH))
        if link is None:
            raise ProtocolError("Geçersiz Meet bağlantısı (örnek: https://meet.google.com/abc-defg-hij)")
        if bot.status in ("connected", "connecting") and bot.meet_link == link:
            return Reply("Bot zaten bu toplantıda")
        log.info("🔗  %s botu toplantıya gönderdi: %s", client.name, link)
        bot.request_join(link)
        return Reply("Toplantıya bağlanılıyor…")

    async def on_leave_meet(client: Client, msg: dict) -> Optional[Reply]:
        previous = bot.status
        if previous == "disconnected":
            raise ProtocolError("Bot zaten bir toplantıda değil")
        log.info("👋  %s botu toplantıdan çıkardı (%s)", client.name, previous)
        await bot.leave()
        return Reply("Katılma iptal edildi" if previous == "connecting" else "Bot toplantıdan ayrıldı")

    # ── Bot ekranı ────────────────────────────────────────────

    async def send_viewers(message: dict) -> None:
        clients = list(viewers.values())
        if clients:
            await asyncio.gather(*(hub.send(c, message) for c in clients))

    async def view_loop() -> None:
        """İzleyici olduğu sürece botun ekranını yollar; değişmeyen kareyi tekrar göndermez."""
        nonlocal view_task
        failures = 0
        try:
            while viewers:
                try:
                    frame = await bot.view_frame()
                except BotError as exc:
                    failures += 1
                    if failures >= VIEW_MAX_FAILURES:
                        log.warning("🖥️  Bot ekranı kapatıldı: %s", exc)
                        await send_viewers({"type": "view_closed", "message": str(exc)})
                        viewers.clear()
                        break
                    if failures == 1:
                        await send_viewers({"type": "view_error", "message": str(exc)})
                    await asyncio.sleep(VIEW_ERROR_RETRY)
                    continue
                failures = 0
                key = (hash(frame.get("image")), frame.get("url"), frame.get("title"), frame.get("signed_in"))
                if key != view_last["key"]:
                    view_last["key"] = key
                    await send_viewers({"type": "view_frame", **frame})
                await asyncio.sleep(VIEW_INTERVAL)
        finally:
            if view_task is asyncio.current_task():
                view_task = None

    def ensure_view_loop() -> None:
        nonlocal view_task
        view_last["key"] = None   # yeni izleyici ilk kareyi hemen alsın
        if view_task is None or view_task.done():
            view_task = spawn(view_loop(), "view-loop")

    async def drop_viewer(client: Client) -> None:
        if viewers.pop(client.id, None) is not None and not viewers:
            try:
                await bot.view_release()
            except BotError as exc:
                log.debug("Bot ekranı bırakılamadı: %s", exc)

    def require_viewer(client: Client) -> None:
        if client.id not in viewers:
            raise ProtocolError("Önce bot ekranını açın")

    async def on_view_start(client: Client, msg: dict) -> Optional[Reply]:
        target = msg.get("target", "meet")
        if target not in VIEW_TARGETS:
            raise ProtocolError("'target' meet ya da login olmalı")
        if not viewers:
            log.info("🖥️  %s bot ekranını açtı (%s)", client.name, target)
        await bot.view_open(target)
        if client.closed:
            return None   # açılırken bağlantı koptu: izleyici olarak kaydetme
        viewers[client.id] = client
        ensure_view_loop()
        return None

    async def on_view_stop(client: Client, msg: dict) -> Optional[Reply]:
        await drop_viewer(client)
        return None

    async def on_view_input(client: Client, msg: dict) -> Optional[Reply]:
        require_viewer(client)
        action = msg.get("action")
        if action not in VIEW_ACTIONS:
            raise ProtocolError("Geçersiz bot ekranı işlemi")
        if action == "click":
            x, y = _number_field(msg, "x"), _number_field(msg, "y")
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ProtocolError("Tıklama konumu 0–1 aralığında olmalı")
            await bot.view_click(x, y)
        elif action == "type":
            text = msg.get("text")
            if not isinstance(text, str) or not text or len(text) > VIEW_MAX_TEXT:
                raise ProtocolError(f"'text' 1–{VIEW_MAX_TEXT} karakterlik bir metin olmalı")
            await bot.view_type(text)   # metin (şifre olabilir) asla günlüğe yazılmaz
        elif action == "key":
            key = msg.get("key")
            if key not in VIEW_KEYS:
                raise ProtocolError("Bu tuş desteklenmiyor")
            await bot.view_key(key)
        elif action == "scroll":
            await bot.view_scroll(_number_field(msg, "dy"))
        elif action == "back":
            await bot.view_back()
        else:
            await bot.view_reload()
        view_last["key"] = None   # sonuç hemen görünsün
        return None

    async def on_google_logout(client: Client, msg: dict) -> Optional[Reply]:
        log.info("🔑  %s botun Google hesabını çıkardı", client.name)
        await bot.google_sign_out()
        view_last["key"] = None   # izleyenler yeni durumu hemen görsün
        return Reply("Botun Google hesabından çıkış yapıldı")

    handlers: dict[str, Handler] = {
        "hello": on_hello, "ping": on_ping, "state": on_state, "auth": on_auth, "logout": on_logout,
        "add": on_add, "remove": on_remove, "move": on_move, "play_now": on_play_now,
        "shuffle": on_shuffle, "clear": on_clear, "pause": on_pause, "resume": on_resume,
        "skip": on_skip, "stop": on_stop, "seek": on_seek, "repeat": on_repeat,
        "volume": on_volume, "mic": on_mic, "join_meet": on_join_meet, "leave_meet": on_leave_meet,
        "view_start": on_view_start, "view_stop": on_view_stop, "view_input": on_view_input,
        "google_logout": on_google_logout,
    }

    # ── Çerçeveler: okuma döngüsü denetler, işçi sırayla yürütür ─
    #
    # Komutlar (atla, ayrıl...) oynatıcı kilidini / tarayıcıyı saniyelerce bekleyebilir.
    # Okuma döngüsü onları beklemez: ping her zaman hemen yanıtlanır (arayüzün kalp
    # atışı kopmaz), komutlar ise bağlantı başına tek bir işçide GELİŞ SIRASIYLA çalışır.

    def parse_rid(msg: dict) -> Optional[str]:
        rid = msg.get("rid")
        if rid is not None and (not isinstance(rid, str) or len(rid) > MAX_RID_LENGTH):
            raise ProtocolError(f"'rid' en fazla {MAX_RID_LENGTH} karakterlik bir metin olmalı")
        return rid

    async def receive_frame(client: Client, text: Optional[str]) -> bool:
        """Tek bir çerçeveyi denetler; komutsa işçinin sırasına koyar (text None → ikili çerçeve).
        False dönerse istemci hız sınırına rağmen mesaj seli yapıyordur: bağlantı kapatılmalı."""
        now = time.monotonic()
        if client.flooding(now):
            return False
        # Hız sınırı, ayrıştırmadan ÖNCE her çerçeveyi sayar (çok büyük, ikili, geçersiz olanlar dahil)
        within_rate = client.allow_message(now)
        rid: Optional[str] = None
        try:
            if text is None:
                raise ProtocolError("Sadece metin (JSON) mesajları kabul edilir")
            if len(text) > MAX_FRAME_BYTES or len(text.encode("utf-8")) > MAX_FRAME_BYTES:
                raise ProtocolError("Mesaj çok büyük (en fazla 16 KB)")
            try:
                msg = json.loads(text)
            except (ValueError, RecursionError):
                raise ProtocolError("Geçersiz JSON") from None
            if not isinstance(msg, dict):
                raise ProtocolError("Mesaj bir JSON nesnesi olmalı")
            rid = parse_rid(msg)  # hız sınırı hatası da (varsa) rid'li ack olarak dönsün
            kind = msg.get("type")
            if kind == "ping" and client.ready:
                # Kalp atışı hız sınırına takılmaz: hata dönseydi arayüz pong alamayıp sağlam
                # bağlantıyı 10 sn sonra koparırdı. (Sel denetimi pingleri de sayar.)
                await on_ping(client, msg)
                await reply_ok(client, rid, None)
                return True
            if not within_rate:
                raise ProtocolError("Çok hızlı mesaj gönderiyorsun, biraz yavaşla")
            if not isinstance(kind, str) or not kind:
                raise ProtocolError("Mesaj türü (type) eksik")
            if client.inbox.qsize() >= MAX_INBOX:
                raise ProtocolError("Sunucu meşgul, önceki işlemlerin bitmesini bekle")
            client.inbox.put_nowait((kind, msg, rid))
        except ProtocolError as exc:
            await reply_error(client, rid, str(exc))
        return True

    async def execute(client: Client, kind: str, msg: dict, rid: Optional[str]) -> None:
        try:
            if not client.ready and kind != "hello":
                raise ProtocolError("Önce 'hello' mesajı gönderilmeli")
            handler = handlers.get(kind)
            if handler is None:
                raise ProtocolError(f"Bilinmeyen mesaj türü: {kind[:32]}")
            # Yetki, işlenme anındaki oturuma göre (önceki auth/logout sıradaysa onlardan sonra)
            if kind != "hello" and kind not in permissions(client):
                if kind in ADMIN_TYPES or (kind in VIEW_TYPES and not client.is_admin):
                    raise ProtocolError("Bu işlem için yönetici yetkisi gerekiyor")
                if kind in VIEW_TYPES:
                    if settings.remote_view == "off":
                        raise ProtocolError("Bot ekranı kapalı (MEETBOT_REMOTE_VIEW=off)")
                    raise ProtocolError("Bot ekranı güvenlik için yalnızca sunucunun kendisinden ya da SSH "
                                        "tüneliyle açılabilir (MEETBOT_REMOTE_VIEW)")
                raise ProtocolError("Bu işlem için yetkin yok (misafir kontrolleri kapalı)")
            reply = await handler(client, msg)
            if kind != "add":  # ekleme yanıtı arka plan görevinden gelir
                await reply_ok(client, rid, reply)
        except (ProtocolError, PlayerError, BotError) as exc:
            await reply_error(client, rid, str(exc))
        except Exception:
            log.exception("❌  '%s' işlenirken beklenmeyen hata (%s)", kind[:32], client.name or client.id)
            await reply_error(client, rid, "Beklenmeyen bir hata oluştu")

    async def command_worker(client: Client) -> None:
        while (command := await client.inbox.get()) is not None:
            await execute(client, *command)

    # ── WebSocket uç noktası ──────────────────────────────────

    async def close_socket(ws: WebSocket, code: int, reason: str) -> None:
        try:
            await asyncio.wait_for(ws.close(code=code, reason=reason), timeout=1.0)
        except Exception as exc:
            log.debug("Bağlantı kapatılamadı: %s", type(exc).__name__)

    async def receive_message(ws: WebSocket, client: Client, hello_deadline: float) -> Optional[dict]:
        """Sıradaki ASGI mesajı; oturum (hello) süresinde açılmadıysa None."""
        loop = asyncio.get_running_loop()
        while not client.ready:
            try:
                return await asyncio.wait_for(ws.receive(), max(0.0, hello_deadline - loop.time()))
            except asyncio.TimeoutError:
                if not client.ready:
                    return None
                # hello tam o sırada işlendi → süresiz beklemeye geç (iptal edilen receive çerçeve kaybetmez)
        return await ws.receive()

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        nonlocal open_sockets
        if not host_header_allowed(ws.headers.get("host"), trusted_hosts):
            log.warning("🚫  Tanınmayan Host başlığı reddedildi (WebSocket): %s", (ws.headers.get("host") or "")[:100])
            await ws.close(code=1008)
            return
        if not origin_allowed(ws.headers):
            log.warning("🚫  Yabancı Origin reddedildi: %s", ws.headers.get("origin"))
            await ws.close(code=1008)
            return
        address = ws.client.host if ws.client is not None and ws.client.host else "?"
        if open_sockets >= MAX_CONNECTIONS:
            log.warning("🚫  Bağlantı sınırı dolu (%d); %s reddedildi", MAX_CONNECTIONS, address)
            await ws.close(code=1013)
            return
        if not is_loopback(address) and sockets_by_address[address] >= MAX_CONNECTIONS_PER_IP:
            log.warning("🚫  %s adresinden çok fazla bağlantı (en fazla %d); yenisi reddedildi",
                        address, MAX_CONNECTIONS_PER_IP)
            await ws.close(code=1013)
            return
        # Sayaçlar ilk await'ten ÖNCE artar: eşzamanlı el sıkışmalar sınırı birlikte aşamasın
        open_sockets += 1
        sockets_by_address[address] += 1
        try:
            await ws.accept()
            await run_session(ws)
        finally:
            open_sockets -= 1
            sockets_by_address[address] -= 1
            if sockets_by_address[address] <= 0:
                del sockets_by_address[address]

    async def run_session(ws: WebSocket) -> None:
        client = Client(ws)
        worker = spawn(command_worker(client), f"ws-worker:{client.id}")
        # Oturum açmayan (hello göndermeyen) bağlantı sınırsız yer tutmasın
        hello_deadline = asyncio.get_running_loop().time() + HELLO_TIMEOUT
        try:
            while True:
                message = await receive_message(ws, client, hello_deadline)
                if message is None:
                    log.info("⏱️  %s: %g sn içinde oturum açılmadı (hello), bağlantı kapatıldı",
                             client.address, HELLO_TIMEOUT)
                    await close_socket(ws, 1008, "Oturum açılmadı (hello)")
                    break
                if message["type"] == "websocket.disconnect":
                    break
                if not await receive_frame(client, message.get("text")):
                    log.warning("🚫  Mesaj seli: %s (%s) bağlantısı kapatıldı", client.name or client.id, client.address)
                    await close_socket(ws, 1008, "Çok fazla mesaj")
                    break
        finally:
            client.closed = True
            client.inbox.put_nowait(None)
            await drop_viewer(client)
            if hub.unregister(client):
                log.info("🔌  %s ayrıldı", client.name)
                await hub.broadcast(hub.listeners_msg())
            # Kopmadan önce gelen komutlar (ör. "atla") yine de sırayla uygulanır. shield: bu
            # uç nokta iptal edilse bile oynatıcı geçişi yarıda kesilmez (işçi kendi biter;
            # yalnızca sunucu kapanışı onu iptal eder).
            await asyncio.shield(worker)

    return app
