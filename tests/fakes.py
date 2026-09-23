"""Test sahteleri: ağsız indirici ve elle kumanda edilen bot."""

from __future__ import annotations

import asyncio
import hashlib
import wave
from pathlib import Path
from typing import Optional

from audio_manager import AudioError, TrackInfo, canonical_watch_url, is_valid_video_id, thumbnail_url
from fake_bot import FakeBot

MEET_LINK = "https://meet.google.com/abc-defg-hij"


def make_info(video_id: str, title: Optional[str] = None, duration: Optional[int] = 180,
              is_live: bool = False) -> TrackInfo:
    return TrackInfo(
        video_id=video_id,
        title=title or f"Şarkı {video_id}",
        duration=duration,
        url=canonical_watch_url(video_id),
        thumbnail=thumbnail_url(video_id),
        is_live=is_live,
    )


def write_wav(path: Path, seconds: float = 1.0, rate: int = 8000) -> Path:
    """Sessiz, 8 bit mono WAV — FakeBot süreyi başlıktan okur."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(1)
        wav.setframerate(rate)
        wav.writeframes(b"\x80" * int(seconds * rate))
    return path


class FakeDownloader:
    """Downloader arayüzü; ağ yok. Gecikme/hata asyncio.Event ve sözlüklerle kontrol edilir.

    • catalog[query] → resolve sonucu (yoksa sorgudan türetilen tek parça)
    • download_gates[video_id] → set edilene kadar indirme bekler
    • download_errors[video_id] → indirme bu hatayı fırlatır
    """

    def __init__(self, downloads_dir: Path, wav_seconds: float = 1.0):
        self.downloads_dir = Path(downloads_dir)
        self.wav_seconds = wav_seconds
        self.catalog: dict[str, list[TrackInfo]] = {}
        self.resolve_error: Optional[AudioError] = None
        self.resolve_delay = 0.0
        self.download_gates: dict[str, asyncio.Event] = {}
        self.download_errors: dict[str, Exception] = {}
        self.resolve_calls: list[str] = []
        self.download_calls: list[str] = []
        self.prioritized: list[str] = []
        self.completed: list[str] = []
        self.cleaned: list[str] = []

    @staticmethod
    def video_id_for(query: str) -> str:
        if is_valid_video_id(query):
            return query
        return hashlib.md5(query.encode("utf-8")).hexdigest()[:11]

    def path_for(self, video_id: str) -> Path:
        return self.downloads_dir / f"{video_id}.wav"

    async def resolve(self, query: str) -> list[TrackInfo]:
        self.resolve_calls.append(query)
        if self.resolve_delay:
            await asyncio.sleep(self.resolve_delay)
        if self.resolve_error is not None:
            raise self.resolve_error
        if query in self.catalog:
            return list(self.catalog[query])
        return [make_info(self.video_id_for(query))]

    async def download(self, video_id: str) -> str:
        self.download_calls.append(video_id)
        gate = self.download_gates.get(video_id)
        if gate is not None:
            await gate.wait()
        error = self.download_errors.get(video_id)
        if error is not None:
            raise error
        path = write_wav(self.path_for(video_id), self.wav_seconds)
        self.completed.append(video_id)
        return str(path)

    def prioritize(self, video_id: str) -> None:
        """Downloader.prioritize gibi: yalnızca kaydeder (sahte indirmede yuva sırası yok)."""
        self.prioritized.append(video_id)

    def cleanup(self, path: str) -> None:
        self.cleaned.append(path)
        Path(path).unlink(missing_ok=True)

    def purge_all(self) -> int:
        count = 0
        for path in self.downloads_dir.glob("*"):
            if path.is_file():
                path.unlink()
                count += 1
        return count


class ScriptedBot(FakeBot):
    """Saat çalışmayan FakeBot: bitiş/ilerleme testten tetiklenir, play hataları sıraya konur."""

    def __init__(self, settings, connected: bool = True):
        super().__init__(settings, join_delay=0.0)
        self.play_calls: list[tuple[str, int, float]] = []
        self.play_errors: list[Exception] = []
        self.play_gate: Optional[asyncio.Event] = None
        self.stop_calls = 0
        self.max_concurrent_plays = 0
        self._plays_in_flight = 0
        if connected:
            self.status = "connected"
            self.meet_link = MEET_LINK

    def _start_clock(self, token: int) -> None:
        """Otomatik saat yok — test finish()/progress() çağırır."""

    async def play(self, file_path: str, token: int, start_at: float = 0.0) -> float:
        self.play_calls.append((file_path, token, start_at))
        self._plays_in_flight += 1
        self.max_concurrent_plays = max(self.max_concurrent_plays, self._plays_in_flight)
        try:
            if self.play_gate is not None:
                await self.play_gate.wait()
            if self.play_errors:
                raise self.play_errors.pop(0)
            return await super().play(file_path, token, start_at)
        finally:
            self._plays_in_flight -= 1

    async def stop(self) -> None:
        self.stop_calls += 1
        await super().stop()

    async def finish(self) -> int:
        """Yüklü parçayı 'bitmiş' say ve on_track_ended'i tetikle."""
        token = self.token
        assert token is not None, "botta yüklü parça yok"
        self._unload()
        await self._call(self.on_track_ended, token)
        return token

    async def report_progress(self, position: float) -> None:
        assert self.token is not None
        self.position = position
        await self._call(self.on_progress, self.token, position, self.duration, self.paused)

    async def connect(self) -> None:
        self.meet_link = MEET_LINK
        await self._set_status("connected", "Toplantıya katıldı")

    async def disconnect(self, detail: str = "Toplantı sona erdi") -> None:
        self._unload()
        await self._set_status("disconnected", detail)


class Recorder:
    """Player/Hub yayınlarını toplayan sahte broadcast."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)

    def of(self, kind: str) -> list[dict]:
        return [m for m in self.messages if m["type"] == kind]

    def last(self, kind: str) -> dict:
        found = self.of(kind)
        assert found, f"'{kind}' mesajı yayınlanmadı"
        return found[-1]


async def wait_until(predicate, timeout: float = 2.0, message: str = "koşul sağlanmadı") -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(message)
        await asyncio.sleep(0.005)


async def settle(rounds: int = 30) -> None:
    """Bekleyen görevlere birkaç tur çalışma fırsatı ver (bir şeyin OLMADIĞINI doğrulamak için)."""
    for _ in range(rounds):
        await asyncio.sleep(0)
