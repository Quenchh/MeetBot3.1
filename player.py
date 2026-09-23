# ──────────────────────────────────────────────────────────────
#  player.py — Parça modeli + oynatıcı durum makinesi
#
#  Durumlar: idle → loading → playing ⇄ paused → (bitti/atla) → loading | idle
#
#  • Tüm durum geçişleri tek bir asyncio.Lock altında yapılır.
#  • Her bot.play çağrısı artan bir "jeton" (token) alır; botun eski jetonlu
#    ended/progress olayları yok sayılır → bir olay asla iki kez ilerletmez.
#  • İndirmeler kilit DIŞINDA beklenir; her geçiş "generation" sayacını artırır,
#    böylece atla/durdur sonrası biten bayat indirmeler hiçbir şey çalmaz.
#  • Bot geri çağrıları (ended/status) kilit beklemeden görev olarak işlenir;
#    botun kendi iç kilitleriyle karşılıklı kilitlenme (deadlock) olamaz.
# ──────────────────────────────────────────────────────────────

from __future__ import annotations

import asyncio
import itertools
import logging
import math
import random
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, Iterable, Optional, Protocol

from audio_manager import MAX_QUERY_LENGTH, AudioError, DownloadError, TrackInfo
from bot import BotError, BotNotReady
from config import Settings

log = logging.getLogger("meetbot.player")

REPEAT_MODES = ("off", "one", "all")
VOLUME_TARGETS = ("music", "mic")
PROGRESS_INTERVAL = 0.9  # saniye — progress yayınları en fazla ~1/sn

Broadcast = Callable[[dict], Awaitable[None]]


# ──────────────────────────────────────────────────────────────
#  Yardımcılar
# ──────────────────────────────────────────────────────────────

def format_duration(seconds: Optional[float]) -> str:
    """183 → "3:03", 3723 → "1:02:03", None → "?"."""
    if seconds is None:
        return "?"
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ──────────────────────────────────────────────────────────────
#  Model
# ──────────────────────────────────────────────────────────────

@dataclass(eq=False)  # kimlik karşılaştırması: aynı alanlara sahip iki parça yine de ayrı nesnedir
class Track:
    id: int
    video_id: str
    title: str
    duration: Optional[int]
    url: str
    thumbnail: str
    added_by: str
    added_at: str
    status: str = "pending"            # pending | downloading | ready | error
    file_path: Optional[str] = None    # ASLA istemcilere gönderilmez
    error: Optional[str] = None        # ASLA istemcilere gönderilmez

    def public(self) -> dict:
        return {
            "id": self.id,
            "video_id": self.video_id,
            "title": self.title,
            "duration": self.duration,
            "duration_str": format_duration(self.duration),
            "url": self.url,
            "thumbnail": self.thumbnail,
            "added_by": self.added_by,
            "added_at": self.added_at,
            "status": self.status,
        }


class PlayerError(Exception):
    """Kullanıcıya gösterilecek Türkçe hata mesajı."""


class DownloaderLike(Protocol):
    async def resolve(self, query: str) -> list[TrackInfo]: ...
    async def download(self, video_id: str) -> str: ...
    def cleanup(self, path: str) -> None: ...
    # İsteğe bağlı: def prioritize(self, video_id: str) -> None — şimdi çalınacak parçanın
    # indirmesi ön-indirmelerin önüne geçer (audio_manager.Downloader sağlar).


class _StaleLoad(Exception):
    """Beklenen indirme iptal edildi (parça artık kuyrukta değil)."""


# ──────────────────────────────────────────────────────────────
#  Player
# ──────────────────────────────────────────────────────────────

class Player:
    def __init__(self, settings: Settings, bot: Any, downloader: DownloaderLike, broadcast: Broadcast):
        self.settings = settings
        self.bot = bot
        self.downloader = downloader
        self._broadcast = broadcast

        self.state = "idle"        # idle | loading | playing | paused
        self.repeat = "off"        # off | one | all
        self.queue: list[Track] = []
        self.current: Optional[Track] = None
        self.history: deque[Track] = deque(maxlen=settings.history_size)
        self.position = 0.0
        self.duration = 0.0
        self.music_volume = 80
        self.mic_volume = 80
        self.mic_muted = False
        self.bot_status: str = bot.status
        self.bot_detail: Optional[str] = bot.status_detail

        self._lock = asyncio.Lock()
        self._ids = itertools.count(1)
        self._tokens = itertools.count(1)
        self._generation = 0                    # her geçişte artar → bayat yüklemeler ayıklanır
        self._active_token: Optional[int] = None  # botta şu an yüklü olan parçanın jetonu
        self._current_started = False           # current gerçekten çalmaya başladı mı (geçmiş için)
        # Kullanıcı bilerek duraklattı mı? (bağlantı kopmasının yol açtığı otomatik duraklatmadan
        # ayırt etmek için: yeniden bağlanınca yalnızca otomatik duraklatılan parça kendiliğinden çalar)
        self._user_paused = False
        self._downloads: dict[str, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self._last_progress = 0.0
        self._closed = False

    # ── Yayın mesajları ───────────────────────────────────────

    def _queue_msg(self) -> dict:
        return {"type": "queue", "queue": [t.public() for t in self.queue]}

    def _playback_fields(self) -> dict:
        return {
            "state": self.state,
            "position": round(self.position, 1),
            "duration": round(self.duration, 1),
            "repeat": self.repeat,
        }

    def _playback_msg(self) -> dict:
        return {"type": "playback", "current": self.current.public() if self.current else None,
                **self._playback_fields()}

    def _progress_msg(self) -> dict:
        return {"type": "progress", "position": round(self.position, 1),
                "duration": round(self.duration, 1), "state": self.state}

    def _history_msg(self) -> dict:
        return {"type": "history", "history": [t.public() for t in self.history]}

    def _volume_msg(self) -> dict:
        return {"type": "volume", "music": self.music_volume, "mic": self.mic_volume}

    def _mic_msg(self) -> dict:
        return {"type": "mic", "muted": self.mic_muted}

    def _bot_fields(self) -> dict:
        return {"status": self.bot_status, "meet_link": self.bot.meet_link, "detail": self.bot_detail}

    def _bot_msg(self) -> dict:
        return {"type": "bot", **self._bot_fields()}

    def snapshot(self) -> dict:
        return {
            "queue": [t.public() for t in self.queue],
            "current": self.current.public() if self.current else None,
            "playback": self._playback_fields(),
            "volume": {"music": self.music_volume, "mic": self.mic_volume},
            "mic_muted": self.mic_muted,
            "bot": self._bot_fields(),
            "history": [t.public() for t in self.history],
        }

    async def _emit(self, message: dict) -> None:
        try:
            await self._broadcast(message)
        except Exception:
            log.exception("⚠️  Yayın gönderilemedi (%s)", message.get("type"))

    async def _publish(self, *kinds: str) -> None:
        builders = {
            "queue": self._queue_msg,
            "playback": self._playback_msg,
            "history": self._history_msg,
            "volume": self._volume_msg,
            "mic": self._mic_msg,
            "bot": self._bot_msg,
        }
        for kind in kinds:
            await self._emit(builders[kind]())

    async def _notice(self, level: str, message: str) -> None:
        log.info("📣  %s", message)
        await self._emit({"type": "notice", "level": level, "message": message})

    # ── Arka plan görevleri ───────────────────────────────────

    def _spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        # İndirme hataları zaten işlenip loglandı; geri kalan her şey beklenmedik bir hatadır
        if exc is not None and not isinstance(exc, AudioError):
            log.error("❌  Arka plan görevi çöktü (%s)", task.get_name(), exc_info=exc)

    async def shutdown(self) -> None:
        """Tüm arka plan görevlerini (indirmeler dahil → yt-dlp süreçleri) iptal eder."""
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── Sorgular ──────────────────────────────────────────────

    def _live_tracks(self) -> list[Track]:
        """Dosyasını tutan parçalar: çalan + kuyruktakiler (geçmiş dosya tutmaz)."""
        return ([self.current] if self.current else []) + self.queue

    def _referenced(self, video_id: str) -> bool:
        return any(t.video_id == video_id for t in self._live_tracks())

    def _find_in_queue(self, track_id: int) -> Track:
        for track in self.queue:
            if track.id == track_id:
                return track
        raise PlayerError("Şarkı kuyrukta bulunamadı")

    def _room_for(self, added_by: str) -> int:
        room = self.settings.max_queue - len(self.queue)
        if self.settings.max_user_queue:
            mine = sum(1 for t in self.queue if t.added_by == added_by)
            room = min(room, self.settings.max_user_queue - mine)
        return max(room, 0)

    def _capacity_error(self, added_by: str) -> PlayerError:
        if len(self.queue) >= self.settings.max_queue:
            return PlayerError(f"Kuyruk dolu (en fazla {self.settings.max_queue} şarkı)")
        return PlayerError(f"Kuyrukta en fazla {self.settings.max_user_queue} şarkın olabilir")

    # ── Dosyalar / indirmeler ─────────────────────────────────

    def _release(self, tracks: Iterable[Track]) -> None:
        """Artık çalan/kuyrukta olmayan parçaların dosyalarını siler, indirmelerini iptal eder."""
        for track in tracks:
            if self._referenced(track.video_id):
                continue
            # Kayıttan hemen çıkar: iptal (yt-dlp ağacının öldürülmesi) birkaç tur sürer;
            # bu arada aynı video yeniden eklenirse ölmekte olan göreve bağlanmasın.
            task = self._downloads.pop(track.video_id, None)
            if task is not None:
                task.cancel()
            if track.file_path:
                self.downloader.cleanup(track.file_path)
                track.file_path = None

    def _ensure_download(self, video_id: str) -> asyncio.Task:
        task = self._downloads.get(video_id)
        if task is None:
            task = self._spawn(self._run_download(video_id), f"download:{video_id}")
            self._downloads[video_id] = task
        for track in self._live_tracks():
            if track.video_id == video_id and track.status != "ready":
                track.status = "downloading"
        return task

    async def _run_download(self, video_id: str) -> str:
        try:
            path = await self.downloader.download(video_id)
        except AudioError as exc:
            error: Optional[str] = str(exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("❌  Beklenmeyen indirme hatası: %s", video_id)
            error = "Şarkı indirilemedi"
        else:
            error = None
        finally:
            if self._downloads.get(video_id) is asyncio.current_task():
                del self._downloads[video_id]

        affected = [t for t in self._live_tracks() if t.video_id == video_id]
        kinds = ("queue", "playback") if any(t is self.current for t in affected) else ("queue",)
        if error is not None:
            for track in affected:
                track.status, track.error = "error", error
            if affected:
                await self._publish(*kinds)
            raise DownloadError(error)
        if not affected:
            # Beklerken kuyruktan çıkarıldı/atlandı → dosyayı tutmaya gerek yok
            self.downloader.cleanup(path)
            return path
        for track in affected:
            track.status, track.file_path, track.error = "ready", path, None
        await self._publish(*kinds)
        return path

    @staticmethod
    def _file_ready(track: Track) -> bool:
        return track.status == "ready" and bool(track.file_path) and Path(track.file_path).is_file()

    def _prioritize_download(self, track: Track) -> None:
        """Şimdi çalınacak parçanın indirmesini, ön-indirmelerden ÖNCE sıraya sokar ve
        indiriciye öncelikli olduğunu bildirir (ön-indirmeler yuvaları doldurmuş olsa bile)."""
        if self._file_ready(track):
            return
        self._ensure_download(track.video_id)
        prioritize = getattr(self.downloader, "prioritize", None)
        if prioritize is not None:
            try:
                prioritize(track.video_id)
            except Exception:
                log.exception("⚠️  İndirme önceliği ayarlanamadı: %s", track.video_id)

    async def _wait_download(self, track: Track) -> str:
        if self._file_ready(track):
            return track.file_path
        task = self._ensure_download(track.video_id)
        await asyncio.wait({task})  # iç görev iptal edilse bile burada istisna fırlamaz
        if task.cancelled():
            raise _StaleLoad()
        return task.result()

    def _prefetch(self) -> None:
        for track in self.queue[: self.settings.prefetch]:
            if track.status == "pending":
                self._ensure_download(track.video_id)

    def _inherit_file_state(self, track: Track) -> None:
        """Aynı video zaten indirildiyse/indiriliyorsa yeni parça onu paylaşır."""
        for other in self._live_tracks():
            if other.video_id == track.video_id and other.status == "ready" and other.file_path:
                track.status, track.file_path = "ready", other.file_path
                return
        if track.video_id in self._downloads:
            track.status = "downloading"

    # ── Geçişler (kilit ALTINDA çağrılır) ─────────────────────

    def _begin(self, track: Track, start_at: float = 0.0) -> None:
        """track'i current yapar ve yükleme görevini başlatır."""
        self._generation += 1
        self.current = track
        self.state = "loading"
        self.position = max(0.0, start_at)
        self.duration = float(track.duration or 0)
        self._active_token = None
        self._user_paused = False
        self._spawn(self._load_and_play(track, self._generation, self.position), f"load:{track.id}")
        # Önce çalınacak parçanın indirmesi: ön-indirmeler (sıradakiler) iki indirme yuvasını
        # kapıp kullanıcının beklediği parçayı arkalarında bekletmesin
        self._prioritize_download(track)
        self._prefetch()

    async def _advance(self) -> None:
        """Sıradaki çalınabilir parçaya geçer (yinelemeli, özyinelemesiz)."""
        self.current = None
        self._active_token = None
        self._user_paused = False
        self.position = 0.0
        self.duration = 0.0
        self.state = "idle"
        while self.queue and self.bot.is_connected:
            track = self.queue.pop(0)
            if track.status == "error":
                self._release([track])
                await self._notice("warning", f"⚠️ {track.title} indirilemedi, atlandı")
                continue
            self._current_started = False
            self._begin(track)
            break
        await self._publish("queue", "playback")

    def _finish_current(self, requeue: bool) -> bool:
        """current'ı bırakır; tekrar-hepsi modunda kuyruğun sonuna yeni kimlikle ekler,
        aksi halde (çaldıysa) geçmişe yazar. Geçmiş değiştiyse True döner."""
        track = self.current
        if track is None:
            return False
        self.current = None
        self._active_token = None
        self._user_paused = False
        self._generation += 1
        if requeue:
            track.id = next(self._ids)
            self.queue.append(track)
            return False
        started = self._current_started
        if started:
            self.history.appendleft(track)
        self._release([track])
        return started

    async def _stop_bot_audio(self) -> None:
        if self._active_token is None:
            return
        try:
            await self.bot.stop()
        except BotNotReady:
            log.debug("Bot toplantıda değil; durdurulacak ses yok")
        except BotError as exc:
            log.warning("⚠️  Bot sesi durduramadı: %s", exc)

    async def _drop_failed_current(self, message: str, error: Optional[str] = None) -> None:
        track = self.current
        if track is not None:
            track.status = "error"
            if error:
                track.error = error
            self.current = None
            self._active_token = None
            self._release([track])
        await self._notice("warning", message)
        await self._advance()

    async def _load_and_play(self, track: Track, generation: int, start_at: float) -> None:
        failure: Optional[str] = None
        path = ""
        try:
            path = await self._wait_download(track)
        except _StaleLoad:
            return
        except DownloadError as exc:
            failure = str(exc)

        async with self._lock:
            if generation != self._generation or self.current is not track:
                log.debug("↩️  Bayat yükleme yok sayıldı: %s", track.title)
                return
            if failure is not None:
                await self._drop_failed_current(f"⚠️ {track.title} indirilemedi, atlandı")
                return
            await self._play_current(track, path, start_at)

    async def _play_current(self, track: Track, path: str, start_at: float) -> None:
        token = next(self._tokens)
        try:
            duration = await self.bot.play(path, token, start_at)
        except BotNotReady as exc:
            log.info("⏸️  Bot hazır değil, parça bekletiliyor (%s): %s", track.title, exc)
            self.state = "paused"
            await self._publish("playback")
            return
        except BotError as exc:
            log.warning("⚠️  Çalınamadı (%s): %s", track.title, exc)
            await self._drop_failed_current(f"⚠️ {track.title} çalınamadı, atlandı: {exc}", str(exc))
            return
        except Exception:
            log.exception("❌  bot.play beklenmedik hata verdi: %s", track.title)
            await self._drop_failed_current(f"⚠️ {track.title} çalınamadı, atlandı", "Çalma hatası")
            return
        self._active_token = token
        self._current_started = True
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0:
            self.duration = float(duration)
        self.state = "playing"
        self._last_progress = time.monotonic()
        log.info("▶️  Çalıyor: %s (jeton %d)", track.title, token)
        await self._publish("playback")

    # ── Kuyruk komutları ──────────────────────────────────────

    async def add(self, query: str, added_by: str) -> list[Track]:
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= MAX_QUERY_LENGTH:
            raise PlayerError(f"Şarkı adı veya bağlantı 1–{MAX_QUERY_LENGTH} karakter olmalı")
        if self._room_for(added_by) == 0:
            raise self._capacity_error(added_by)

        try:
            infos = await self.downloader.resolve(query.strip())
        except AudioError as exc:
            raise PlayerError(str(exc)) from None
        except Exception:
            log.exception("❌  Çözümleme beklenmedik hata verdi: %r", query)
            raise PlayerError("Şarkı bilgisi alınamadı") from None

        skipped: Counter[str] = Counter()
        accepted: list[TrackInfo] = []
        max_duration = self.settings.max_duration
        for info in infos[: self.settings.playlist_limit]:
            if info.is_live:
                skipped["live"] += 1
            elif max_duration and info.duration is not None and info.duration > max_duration:
                skipped["long"] += 1
            else:
                accepted.append(info)

        async with self._lock:
            room = self._room_for(added_by)
            if len(accepted) > room:
                skipped["full"] += len(accepted) - room
                accepted = accepted[:room]
            if not accepted:
                raise self._rejection(skipped, added_by)

            tracks = []
            for info in accepted:
                track = Track(
                    id=next(self._ids), video_id=info.video_id, title=info.title,
                    duration=info.duration, url=info.url, thumbnail=info.thumbnail,
                    added_by=added_by, added_at=utc_now_iso(),
                )
                self._inherit_file_state(track)
                tracks.append(track)
            self.queue.extend(tracks)
            await self._notice(*self._added_notice(tracks, skipped, added_by))

            if self.current is None and self.state == "idle" and self.bot.is_connected:
                await self._advance()
            else:
                self._prefetch()
                await self._publish("queue")
        return tracks

    def _rejection(self, skipped: Counter, added_by: str) -> PlayerError:
        if skipped["full"]:
            return self._capacity_error(added_by)
        if skipped["long"]:
            return PlayerError(f"Şarkı çok uzun (en fazla {format_duration(self.settings.max_duration)})")
        if skipped["live"]:
            return PlayerError("Canlı yayınlar eklenemez")
        return PlayerError("Sonuç bulunamadı")

    @staticmethod
    def _added_notice(tracks: list[Track], skipped: Counter, added_by: str) -> tuple[str, str]:
        what = tracks[0].title if len(tracks) == 1 else f"{len(tracks)} şarkı"
        message = f"🎵 {added_by}: {what} kuyruğa eklendi"
        reasons = [
            f"{skipped[key]} {label}"
            for key, label in (("long", "çok uzun"), ("live", "canlı yayın"), ("full", "kuyruk sınırı"))
            if skipped[key]
        ]
        if not reasons:
            return "success", message
        return "warning", f"{message} ({sum(skipped.values())} şarkı atlandı: {', '.join(reasons)})"

    async def remove(self, track_id: int, requested_by: Optional[str] = None, force: bool = False) -> Track:
        async with self._lock:
            track = self._find_in_queue(track_id)
            if not force and track.added_by != requested_by:
                raise PlayerError("Sadece kendi eklediğin şarkıları kaldırabilirsin")
            self.queue.remove(track)
            self._release([track])
            self._prefetch()
            log.info("🗑️  Kuyruktan çıkarıldı: %s", track.title)
            await self._publish("queue")
            return track

    async def move(self, track_id: int, index: int) -> None:
        async with self._lock:
            track = self._find_in_queue(track_id)
            index = max(0, min(int(index), len(self.queue) - 1))
            self.queue.remove(track)
            self.queue.insert(index, track)
            self._prefetch()
            await self._publish("queue")

    async def play_now(self, track_id: int) -> None:
        async with self._lock:
            track = self._find_in_queue(track_id)
            if not self.bot.is_connected:
                raise PlayerError("Bot toplantıda değil")
            self.queue.remove(track)
            self.queue.insert(0, track)
            await self._stop_bot_audio()
            history_changed = self._finish_current(requeue=self.repeat == "all")
            await self._advance()
            if history_changed:
                await self._publish("history")

    async def clear(self) -> None:
        async with self._lock:
            removed, self.queue = self.queue, []
            self._release(removed)
            log.info("🧹  Kuyruk temizlendi (%d şarkı)", len(removed))
            await self._publish("queue")

    async def shuffle(self) -> None:
        async with self._lock:
            random.shuffle(self.queue)
            self._prefetch()
            await self._publish("queue")

    # ── Oynatma komutları ─────────────────────────────────────

    async def pause(self) -> None:
        async with self._lock:
            if self.state == "paused":
                # Bağlantı kopunca otomatik duraklamıştı: kullanıcı da duraklatmak istiyor →
                # yeniden bağlanınca kendiliğinden çalmasın
                self._user_paused = self.current is not None
                return
            if self.state != "playing":
                raise PlayerError("Şarkı henüz yükleniyor" if self.state == "loading" else "Şu an çalan bir şarkı yok")
            try:
                await self.bot.pause()
            except BotNotReady:
                self._active_token = None  # botta ses yok; devam edince baştan yüklenecek
            except BotError as exc:
                raise PlayerError(str(exc)) from None
            self.state = "paused"
            self._user_paused = True
            await self._publish("playback")

    async def resume(self) -> None:
        async with self._lock:
            if self.state in ("playing", "loading"):
                return
            if not self.bot.is_connected:
                raise PlayerError("Bot toplantıda değil")
            if self.current is not None:
                if self._active_token is not None:
                    try:
                        await self.bot.resume()
                    except BotError as exc:
                        raise PlayerError(str(exc)) from None
                    self.state = "playing"
                    self._user_paused = False
                else:
                    self._begin(self.current, start_at=self.position)
                await self._publish("playback")
                return
            if not self.queue:
                raise PlayerError("Kuyruk boş")
            await self._advance()

    async def skip(self) -> None:
        async with self._lock:
            if self.current is None:
                raise PlayerError("Şu an çalan bir şarkı yok")
            log.info("⏭️  Geçiliyor: %s", self.current.title)
            await self._stop_bot_audio()
            history_changed = self._finish_current(requeue=self.repeat == "all")
            await self._advance()
            if history_changed:
                await self._publish("history")

    async def stop(self) -> None:
        async with self._lock:
            if self.current is None:
                return
            log.info("⏹️  Durduruldu: %s", self.current.title)
            await self._stop_bot_audio()
            history_changed = self._finish_current(requeue=False)
            self.state = "idle"
            self.position = self.duration = 0.0
            await self._publish("queue", "playback")
            if history_changed:
                await self._publish("history")

    async def seek(self, position: float) -> None:
        if isinstance(position, bool) or not isinstance(position, (int, float)) or not math.isfinite(position):
            raise PlayerError("Geçersiz konum")
        async with self._lock:
            if self.current is None:
                raise PlayerError("Şu an çalan bir şarkı yok")
            if self.state == "loading":
                raise PlayerError("Şarkı henüz yükleniyor")
            target = max(0.0, float(position))
            if self.duration > 0:
                target = min(target, self.duration)
            if self._active_token is not None:
                try:
                    await self.bot.seek(target)
                except BotNotReady:
                    self._active_token = None
                except BotError as exc:
                    raise PlayerError(str(exc)) from None
            self.position = target
            self._last_progress = time.monotonic()
            await self._emit(self._progress_msg())

    async def set_repeat(self, mode: str) -> None:
        if mode not in REPEAT_MODES:
            raise PlayerError("Geçersiz tekrar modu")
        async with self._lock:
            self.repeat = mode
            await self._publish("playback")

    async def set_volume(self, target: str, value: int) -> None:
        if target not in VOLUME_TARGETS:
            raise PlayerError("Geçersiz ses hedefi")
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
            raise PlayerError("Ses seviyesi 0–100 arasında bir tam sayı olmalı")
        async with self._lock:
            setter = self.bot.set_music_volume if target == "music" else self.bot.set_mic_volume
            error: Optional[str] = None
            try:
                await setter(value)
            except BotNotReady:
                log.debug("Bot toplantıda değil; %s sesi katılınca uygulanacak", target)
            except BotError as exc:
                log.warning("⚠️  Ses seviyesi uygulanamadı (%s): %s", target, exc)
                error = str(exc)
            if target == "music":
                self.music_volume = value
            else:
                self.mic_volume = value
            await self._publish("volume")
            if error:
                raise PlayerError(error)

    async def set_mic_muted(self, muted: bool) -> None:
        if not isinstance(muted, bool):
            raise PlayerError("Geçersiz mikrofon durumu")
        async with self._lock:
            try:
                self.mic_muted = bool(await self.bot.set_mic_muted(muted))
            except BotNotReady:
                self.mic_muted = muted  # toplantıya girince uygulanır
            except BotError as exc:
                raise PlayerError(str(exc)) from None
            await self._publish("mic")

    # ── Bot olayları (main.py bağlar) ─────────────────────────

    async def on_bot_status(self, status: str, detail: Optional[str]) -> None:
        if self._closed:
            return
        # Durum hemen yayınlanır (kilit gerekmez; bot olayları sırayla bekler). Oynatma
        # geçişi kilit altında ayrı görevde: uzun bir bot.play yayını geciktirmesin.
        self.bot_status, self.bot_detail = status, detail
        log.info("🤖  Bot durumu: %s%s", status, f" ({detail})" if detail else "")
        await self._publish("bot")
        self._spawn(self._handle_bot_status(status), f"bot-status:{status}")

    async def on_track_ended(self, token: int) -> None:
        if not self._closed:
            self._spawn(self._handle_track_ended(token), f"ended:{token}")

    async def on_progress(self, token: int, position: float, duration: float, paused: bool) -> None:
        if token != self._active_token or self.current is None:
            return
        if isinstance(position, (int, float)) and math.isfinite(position):
            self.position = max(0.0, float(position))
        if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0:
            self.duration = float(duration)
        now = time.monotonic()
        if now - self._last_progress < PROGRESS_INTERVAL:
            return
        self._last_progress = now
        await self._emit(self._progress_msg())

    async def _reapply_bot_settings(self) -> None:
        """Yeni sayfada ses seviyeleri/mikrofon varsayılana döner → son değerleri uygula.
        Her ayar ayrı denenir: biri başarısız olsa da diğerleri uygulanır."""
        for name, setter, value in (
            ("müzik sesi", self.bot.set_music_volume, self.music_volume),
            ("mikrofon sesi", self.bot.set_mic_volume, self.mic_volume),
        ):
            try:
                await setter(value)
            except BotError as exc:
                log.warning("⚠️  %s yeniden uygulanamadı: %s", name, exc)
        try:
            muted = bool(await self.bot.set_mic_muted(self.mic_muted))
        except BotError as exc:
            log.warning("⚠️  Mikrofon durumu yeniden uygulanamadı: %s", exc)
            return
        if muted != self.mic_muted:
            self.mic_muted = muted
            await self._publish("mic")

    async def _handle_bot_status(self, status: str) -> None:
        async with self._lock:
            if status == "connected":
                if not self.bot.is_connected:
                    return  # bayat olay: kilidi beklerken bot yine koptu; sonraki bağlantıda devam
                await self._reapply_bot_settings()
                if self.current is not None:
                    if self._active_token is None and self.state == "paused" and not self._user_paused:
                        # Botta ses yok (yeni sayfa) → kaldığı yerden yeniden yükle. Kullanıcı
                        # bilerek duraklattıysa duraklatılmış kalır; "devam" edince kaldığı yerden yüklenir.
                        self._begin(self.current, start_at=self.position)
                        await self._publish("playback")
                elif self.queue and self.state == "idle":
                    await self._advance()
            elif self.current is not None and (
                self.state in ("playing", "loading") or self._active_token is not None
            ):
                # Bağlantı gitti → sayfadaki ses de gitti; konumu hatırla, bekle
                self.state = "paused"
                self._active_token = None
                self._generation += 1
                await self._publish("playback")

    async def _handle_track_ended(self, token: int) -> None:
        async with self._lock:
            if token != self._active_token or self.current is None:
                log.debug("↩️  Bayat 'bitti' olayı yok sayıldı (jeton %s)", token)
                return
            track = self.current
            if self.repeat == "one":
                self._begin(track)
                await self._publish("playback")
                return
            history_changed = self._finish_current(requeue=self.repeat == "all")
            await self._advance()
            if history_changed:
                await self._publish("history")
