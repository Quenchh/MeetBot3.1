# ──────────────────────────────────────────────────────────────
#  fake_bot.py — Chrome'suz sahte bot (geliştirme/demo + testler)
#
#  MeetBot ile aynı arayüz (bkz. SPEC §2): sahte toplantıya katılma ve
#  gerçek zamanlı ilerleyen bir oynatma saati. Süre WAV başlığından, WAV
#  değilse (webm/m4a/opus) ffprobe ile okunur; ikisi de olmazsa varsayılan
#  (30 sn) kullanılır. `speed` saati hızlandırır.
#
#  Kullanım:  python main.py --fake-bot   (tam arayüz, gerçek indirici, Meet yok)
# ──────────────────────────────────────────────────────────────

from __future__ import annotations

import asyncio
import base64
import logging
import math
import shutil
import struct
import subprocess
import wave
import zlib
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from bot import VIEW_KEYS, VIEW_MAX_TEXT, VIEW_TARGETS, BotError, BotNotReady
from config import Settings

log = logging.getLogger("meetbot.fakebot")

DEFAULT_DURATION = 30.0
FFPROBE_TIMEOUT = 10.0  # sn


def wav_duration(path: str) -> Optional[float]:
    """WAV dosyasının süresi (sn); WAV değilse veya okunamazsa None."""
    try:
        with wave.open(path, "rb") as wav:
            rate = wav.getframerate()
            return wav.getnframes() / rate if rate else None
    except (wave.Error, EOFError, OSError):
        return None


def ffprobe_duration(path: str) -> Optional[float]:
    """ffprobe ile ses dosyasının süresi (sn); ffprobe yoksa veya okunamazsa None.

    --fake-bot modunda gerçek indirici webm/m4a indirir; süre okunmazsa arayüz her
    şarkıyı 0:30 gösterirdi. Bloklar: olay döngüsünden asyncio.to_thread ile çağrılır.
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", "-i", str(Path(path).resolve())],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=FFPROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("⚠️  ffprobe çalıştırılamadı: %s", exc)
        return None
    try:
        duration = float(result.stdout.strip().splitlines()[0]) if result.returncode == 0 else math.nan
    except (ValueError, IndexError):
        duration = math.nan
    return duration if math.isfinite(duration) and duration > 0 else None


def placeholder_png(width: int = 640, height: int = 360, rgb: tuple[int, int, int] = (75, 0, 130)) -> bytes:
    """Tek renkli PNG (bağımlılıksız): sahte botun "bot ekranı" görüntüsü."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


class FakeBot:
    def __init__(
        self,
        settings: Settings,
        *,
        speed: float = 1.0,
        tick: float = 1.0,
        join_delay: float = 1.5,
        default_duration: float = DEFAULT_DURATION,
    ):
        self.settings = settings
        self.speed = speed                  # 1 gerçek sn'de ilerleyen sahte sn
        self.tick = tick                    # ilerleme bildirimi aralığı (gerçek sn)
        self.join_delay = join_delay
        self.default_duration = default_duration

        # §2 salt-okunur durum
        self.status = "disconnected"
        self.meet_link: Optional[str] = None
        self.status_detail: Optional[str] = None

        # §2 geri çağrılar (main.py atar)
        self.on_status: Optional[Callable[[str, Optional[str]], Awaitable[None]]] = None
        self.on_track_ended: Optional[Callable[[int], Awaitable[None]]] = None
        self.on_progress: Optional[Callable[[int, float, float, bool], Awaitable[None]]] = None

        # Ses durumu (gerçek bot gibi son değerler saklanır)
        self.music_volume = 80
        self.mic_volume = 80
        self.mic_muted = False
        self.token: Optional[int] = None    # yüklü parçanın jetonu
        self.file_path: Optional[str] = None
        self.position = 0.0
        self.duration = 0.0
        self.paused = False

        self._join_task: Optional[asyncio.Task] = None
        self._clock_task: Optional[asyncio.Task] = None
        self._join_requests = 0             # her yeni katılım isteğinde artar (leave yarışı için)

        # Bot ekranı: gerçek tarayıcı yok; yer tutucu kare + gelen olayların kaydı (testler için)
        self.view_target: Optional[str] = None
        self.view_events: list[tuple] = []
        self.signed_in: Optional[bool] = False
        self._view_image = "data:image/png;base64," + base64.b64encode(placeholder_png()).decode("ascii")

    @property
    def is_connected(self) -> bool:
        return self.status == "connected"

    # ── Yardımcılar ───────────────────────────────────────────

    async def _call(self, callback: Optional[Callable[..., Awaitable[None]]], *args: Any) -> None:
        if callback is None:
            return
        try:
            await callback(*args)
        except Exception:
            log.exception("⚠️  Sahte bot geri çağrısı hata verdi")

    async def _set_status(self, status: str, detail: Optional[str]) -> None:
        self.status, self.status_detail = status, detail
        log.info("🤖  [sahte bot] %s — %s", status, detail or "")
        await self._call(self.on_status, status, detail)

    def _require_connected(self) -> None:
        if not self.is_connected:
            raise BotNotReady("Bot toplantıda değil")

    def _unload(self) -> None:
        """Yüklü parçayı bırakır; bu jeton için artık progress/ended gelmez."""
        clock = self._clock_task
        if clock is not None and clock is not asyncio.current_task():
            clock.cancel()
        self._clock_task = None
        self.token = None
        self.file_path = None
        self.position = self.duration = 0.0
        self.paused = False

    def _start_clock(self, token: int) -> None:
        self._clock_task = asyncio.get_running_loop().create_task(self._clock(token), name=f"fakebot-clock:{token}")

    async def _clock(self, token: int) -> None:
        while self.token == token:
            await asyncio.sleep(self.tick)
            if self.token != token:
                return
            if not self.paused:
                self.position = min(self.duration, self.position + self.tick * self.speed)
            await self._call(self.on_progress, token, self.position, self.duration, self.paused)
            if self.token == token and self.position >= self.duration:
                self._unload()
                await self._call(self.on_track_ended, token)
                return

    # ── Toplantı ──────────────────────────────────────────────

    def request_join(self, link: str) -> None:
        """Gerçek bot gibi: aynı toplantı için no-op; durum hemen "connecting" olur
        (ses komutları BotNotReady alır), olay ise katılım görevinden yayınlanır."""
        if link == self.meet_link and self.status in ("connecting", "connected"):
            return
        if self._join_task is not None and not self._join_task.done():
            self._join_task.cancel()
        self._join_requests += 1
        self.status, self.meet_link = "connecting", link
        self._join_task = asyncio.get_running_loop().create_task(self._join(link), name="fakebot-join")

    async def _join(self, link: str) -> None:
        self._unload()  # başka toplantıdaysa sayfa (ve ses) gider
        await self._set_status("connecting", "Toplantıya bağlanılıyor (sahte bot)")
        await asyncio.sleep(self.join_delay)
        await self._set_status("connected", "Toplantıya katıldı (sahte bot)")

    async def _cancel_join(self) -> None:
        task, self._join_task = self._join_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def leave(self) -> None:
        previous = self.status
        requests = self._join_requests
        await self._cancel_join()
        if self._join_requests != requests:
            # Gerçek bot gibi: leave() başladıktan SONRA gelen request_join() kazanır; yeni
            # katılımın durumunu ezme, bayat "disconnected" olayı da gönderme
            log.info("🤖  [sahte bot] Ayrılma, yeni bir katılım isteğiyle geçersiz kaldı")
            return
        self._unload()
        self.status, self.meet_link = "disconnected", None
        if previous != "disconnected":
            await self._set_status("disconnected",
                                   "Katılma iptal edildi" if previous == "connecting" else "Toplantıdan ayrıldı")

    # ── Ses ───────────────────────────────────────────────────

    async def play(self, file_path: str, token: int, start_at: float = 0.0) -> float:
        self._require_connected()
        if not Path(file_path).is_file():
            raise BotError("Ses dosyası bulunamadı")
        duration = wav_duration(file_path)
        if not duration:
            duration = await asyncio.to_thread(ffprobe_duration, file_path) or self.default_duration
            self._require_connected()  # süre okunurken toplantıdan çıkıldıysa (gerçek bot gibi)
        self._unload()  # yeni jeton eskisini geçersiz kılar
        self.token = token
        self.file_path = file_path
        self.duration = duration
        self.position = min(max(0.0, start_at), duration)
        self._start_clock(token)
        return duration

    async def pause(self) -> None:
        self._require_connected()
        self.paused = True

    async def resume(self) -> None:
        self._require_connected()
        self.paused = False

    async def stop(self) -> None:
        self._unload()

    async def seek(self, position: float) -> None:
        self._require_connected()
        if self.token is None:
            raise BotError("Çalan bir şarkı yok")
        self.position = min(max(0.0, position), self.duration)

    async def set_music_volume(self, value: int) -> None:
        self.music_volume = value

    async def set_mic_volume(self, value: int) -> None:
        self.mic_volume = value

    async def set_mic_muted(self, muted: bool) -> bool:
        self.mic_muted = muted
        return muted

    async def shutdown(self) -> None:
        clock = self._clock_task
        await self.leave()  # iptal edilen katılım beklenir; saat durur
        if self._join_task is not None:
            await self.leave()  # ayrılırken yeni bir katılım başladıysa onu da iptal et
        if clock is not None:
            await asyncio.gather(clock, return_exceptions=True)

    # ── Bot ekranı (sahte) ────────────────────────────────────

    async def view_open(self, target: str) -> None:
        if target not in VIEW_TARGETS:
            raise BotError("Geçersiz bot ekranı hedefi")
        if target == "login" and self.signed_in:
            self.view_target = "meet"
            raise BotError("Google hesabı zaten bağlı (başka bir hesap için önce hesabı çıkarın)")
        self.view_target = target
        self.view_events.append(("open", target))

    async def view_release(self) -> None:
        self.view_events.append(("release",))

    async def view_frame(self) -> dict:
        if self.view_target is None:
            raise BotError("Botun tarayıcısı açık değil (bot ekranını yeniden açın)")
        if self.view_target == "login" and self.signed_in:
            self.view_target = "meet"   # gerçek bot gibi: oturum açılınca giriş sekmesi kapanır
            self.view_events.append(("login_closed",))
        login = self.view_target == "login"
        return {
            "target": self.view_target,
            "interactive": login,
            "image": self._view_image,
            "width": 640,
            "height": 360,
            "url": "https://accounts.google.com/" if login else (self.meet_link or "about:blank"),
            "title": "Google Hesabı (sahte bot)" if login else "Meet (sahte bot)",
            "signed_in": self.signed_in,
        }

    def _require_login_view(self) -> None:
        """Gerçek bot gibi: girdi yalnızca (oturum açılmamış) giriş sekmesine gider."""
        if self.view_target != "login":
            raise BotError("Meet sekmesi yalnızca izlenebilir; tıklama ve yazma yalnızca Google girişi sekmesinde")
        if self.signed_in:
            self.view_target = "meet"
            raise BotError("Google hesabı bağlandı; giriş sekmesi güvenlik için kapatıldı")

    async def view_click(self, x: float, y: float) -> None:
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise BotError("Geçersiz tıklama konumu")
        self._require_login_view()
        self.view_events.append(("click", x, y))

    async def view_type(self, text: str) -> None:
        if not text or len(text) > VIEW_MAX_TEXT:
            raise BotError(f"Metin 1–{VIEW_MAX_TEXT} karakter olmalı")
        self._require_login_view()
        self.view_events.append(("type", text))

    async def view_key(self, key: str) -> None:
        if key not in VIEW_KEYS:
            raise BotError("Bu tuş desteklenmiyor")
        self._require_login_view()
        self.view_events.append(("key", key))

    async def view_scroll(self, dy: float) -> None:
        self._require_login_view()
        self.view_events.append(("scroll", dy))

    async def view_back(self) -> None:
        self._require_login_view()
        self.view_events.append(("back",))

    async def view_reload(self) -> None:
        self._require_login_view()
        self.view_events.append(("reload",))

    async def google_sign_out(self) -> None:
        if self.status != "disconnected":
            raise BotError("Önce botu toplantıdan çıkarın")
        self.signed_in = False
        self.view_target = "meet" if self.view_target else self.view_target
        self.view_events.append(("sign_out",))
