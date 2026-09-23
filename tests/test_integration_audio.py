"""Ses zinciri entegrasyonu: GERÇEK MeetBot + GERÇEK Player, sahte Meet sayfasına karşı.

• Bot: bot.py'nin MeetBot'u; Chrome/CDP yerine _ensure_context() dikişiyle Playwright'ın
  Chromium bağlamını kullanır (test_bot_flow.py ile aynı yöntem). https://meet.google.com/**
  istekleri tests/mock_meet/meet.html'e yönlendirilir; meetbot_inject.js gerçekten enjekte edilir.
• İndirici: FakeDownloader; her şarkı için FARKLI uzunlukta, duyulabilir bir sinüs WAV'ı yazar.
• Player geri çağrıları main.py'deki gibi bağlanır. Sesin gerçekten aktığı, sayfada
  MediaStreamDestination akışına bağlanan bir AnalyserNode ile (RMS) ölçülür.
"""

from __future__ import annotations

import array
import asyncio
import json
import math
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

import fake_bot
from bot import MeetBot
from fake_bot import FakeBot
from fakes import MEET_LINK, FakeDownloader, Recorder, make_info, settle, wait_until
from player import Player

MOCK_HTML = (Path(__file__).parent / "mock_meet" / "meet.html").read_text(encoding="utf-8")

# video_id → (başlık = arama metni, süre sn, frekans Hz)
TRACKS = {
    "vidKisa0001": ("Kısa Şarkı", 3.0, 440.0),
    "vidOrta0001": ("Orta Şarkı", 6.0, 550.0),
    "vidUzun0001": ("Uzun Şarkı", 12.0, 660.0),
    "vidSonra001": ("Sonraki Şarkı", 9.0, 330.0),
    "vidBozuk001": ("Bozuk Şarkı", 3.0, 0.0),
}
LOUD = 0.05     # müzik çalarken hedef akıştaki RMS bunun üstünde (0.5 genlikli sinüs ≈ 0.1–0.35)
SILENT = 0.01

# Botun ses motorunun çıkışına (Meet'e giden MediaStreamDestination) ayrı bir AudioContext'te dinleyici
TAP_JS = """() => {
    const { dest } = window.__meetbot.engine;   // Meet'in getUserMedia çağrısıyla kuruldu
    const ctx = new AudioContext();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 2048;
    ctx.createMediaStreamSource(dest.stream).connect(analyser);
    window.__destTap = { ctx, analyser };
}"""

LEVEL_JS = """async (ms) => {
    await new Promise((resolve) => setTimeout(resolve, ms));
    const { analyser } = window.__destTap;
    const data = new Float32Array(analyser.fftSize);
    analyser.getFloatTimeDomainData(data);
    return Math.sqrt(data.reduce((sum, x) => sum + x * x, 0) / data.length);
}"""


def write_tone(path: Path, seconds: float, freq: float, rate: int = 24_000, amplitude: float = 0.5) -> Path:
    peak = 32767 * amplitude
    samples = array.array("h", (int(peak * math.sin(2 * math.pi * freq * i / rate))
                                for i in range(int(seconds * rate))))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())
    return path


class ToneDownloader(FakeDownloader):
    """FakeDownloader; sessiz 8 kHz WAV yerine her şarkıya kendi süresinde bir sinüs WAV'ı yazar.
    `corrupt` içindeki şarkılar için tarayıcının çözemeyeceği bir dosya döner."""

    def __init__(self, downloads_dir: Path) -> None:
        super().__init__(downloads_dir)
        self.corrupt: set[str] = set()
        for video_id, (title, seconds, _) in TRACKS.items():
            self.catalog[title] = [make_info(video_id, title, math.ceil(seconds))]

    async def download(self, video_id: str) -> str:
        self.download_calls.append(video_id)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        if video_id in self.corrupt:
            path = self.downloads_dir / f"{video_id}.webm"
            path.write_bytes(b"bu bir ses dosyasi degil" * 200)
        else:
            _, seconds, freq = TRACKS[video_id]
            path = await asyncio.to_thread(write_tone, self.path_for(video_id), seconds, freq)
        self.completed.append(video_id)
        return str(path)


class PageBot(MeetBot):
    """Chrome yerine testin Playwright bağlamını kullanan, hızlandırılmış gerçek MeetBot."""

    MONITOR_INTERVAL = 0.1
    OUT_OF_CALL_LIMIT = 3
    PREJOIN_TIMEOUT = 10.0
    POLL_INTERVAL = 0.05
    JOIN_RETRY_DELAY = 0.05

    def __init__(self, settings, context) -> None:
        super().__init__(settings)
        self.test_context = context
        self.play_calls: list[tuple[str, int, float]] = []   # (dosya adı, jeton, başlangıç)

    async def _ensure_context(self, announce=True):
        return self.test_context

    async def play(self, file_path: str, token: int, start_at: float = 0.0) -> float:
        self.play_calls.append((Path(file_path).name, token, start_at))
        return await super().play(file_path, token, start_at)


async def serve_mock_meet(route) -> None:
    if route.request.resource_type != "document":
        await route.fulfill(status=404, body="")
        return
    scenario = json.dumps({"admission": "direct", "popup": False})
    await route.fulfill(status=200, content_type="text/html; charset=utf-8",
                        body=MOCK_HTML.replace("__SCENARIO__", scenario))


class Chain:
    """bot + player + indirici; main.py'deki gibi bağlanmış. `ended`: botun bildirdiği bitişler."""

    def __init__(self, settings, context) -> None:
        self.settings = settings
        self.bot = PageBot(settings, context)
        self.downloader = ToneDownloader(settings.downloads_dir)
        self.recorder = Recorder()
        self.player = Player(settings, self.bot, self.downloader, self.recorder)
        self.ended: list[int] = []

        async def on_track_ended(token: int) -> None:
            self.ended.append(token)
            await self.player.on_track_ended(token)

        self.bot.on_status = self.player.on_bot_status
        self.bot.on_track_ended = on_track_ended
        self.bot.on_progress = self.player.on_progress

    @property
    def page(self):
        return self.bot._page

    async def join(self) -> None:
        self.bot.request_join(MEET_LINK)
        await wait_until(lambda: self.bot.is_connected and self.player.bot_status == "connected",
                         timeout=15, message="bot toplantıya katılmadı")
        await self.page.evaluate(TAP_JS)   # her katılımda sayfa yeniden yüklenir → yeni dinleyici

    async def wait_playing(self, title: str, timeout: float = 10.0) -> None:
        player = self.player
        await wait_until(lambda: player.state == "playing" and player.current is not None
                         and player.current.title == title, timeout=timeout,
                         message=f"'{title}' çalmaya başlamadı")

    async def page_audio(self) -> dict:
        return await self.page.evaluate("() => window.__meetbot.status()")

    async def level(self, ms: int = 200) -> float:
        return await self.page.evaluate(LEVEL_JS, ms)

    async def wait_level(self, predicate, what: str, timeout: float = 4.0) -> float:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            rms = await self.level()
            if predicate(rms):
                return rms
            if loop.time() > deadline:
                raise AssertionError(f"{what}: RMS={rms:.4f}")

    def titles(self, tracks) -> list[str]:
        return [track.title for track in tracks]


@pytest.fixture
async def chain(chromium, settings):
    context = await chromium.new_context()
    await context.route("https://meet.google.com/**", serve_mock_meet)
    chain = Chain(settings, context)
    try:
        yield chain
    finally:
        await chain.player.shutdown()
        await chain.bot.shutdown()
        await context.close()


# ──────────────────────────────────────────────────────────────
#  Testler
# ──────────────────────────────────────────────────────────────

@pytest.mark.browser
async def test_join_autostart_progress_natural_end_and_next_track(chain):
    player, bot = chain.player, chain.bot
    await player.add("Kısa Şarkı", "Vedat")
    await settle()
    assert player.state == "idle" and bot.play_calls == []   # bot toplantıda değil → çalmaz

    await chain.join()   # "connected" olayı kuyruğu başlatır; bot dosyayı sayfaya yükleyip çalar
    await chain.wait_playing("Kısa Şarkı")
    first = player._active_token
    assert bot.play_calls == [("vidKisa0001.wav", first, 0.0)]
    assert (await chain.page_audio())["token"] == first
    assert player.duration == pytest.approx(3.0, abs=0.05)   # süreyi sayfadaki <audio> bildirdi
    await chain.wait_level(lambda rms: rms > LOUD, "müzik MediaStreamDestination'a akmıyor")
    assert await chain.page.evaluate("() => mock.level(250)") > 0.02   # Meet'in aldığı mikrofon izi

    await player.add("Orta Şarkı", "Ayşe")   # çalarken eklenen kuyrukta bekler
    assert player.current.title == "Kısa Şarkı" and chain.titles(player.queue) == ["Orta Şarkı"]

    # Botun ilerleme bildirimleri oynatıcıya (ve yayına) ulaşır
    await wait_until(lambda: player.position > 0.5, timeout=5, message="konum ilerlemedi")
    await wait_until(lambda: chain.recorder.of("progress"), timeout=3, message="progress yayını yok")
    positions = [m["position"] for m in chain.recorder.of("progress")]
    assert positions == sorted(positions) and positions[-1] > 0

    # Doğal bitiş → sıradaki şarkı YENİ jetonla başlar; tek ilerleme
    await chain.wait_playing("Orta Şarkı", timeout=8)
    second = player._active_token
    assert second > first
    assert chain.ended == [first]
    assert chain.titles(player.history) == ["Kısa Şarkı"] and player.queue == []
    assert [call[:2] for call in bot.play_calls] == [("vidKisa0001.wav", first), ("vidOrta0001.wav", second)]
    assert (await chain.page_audio())["token"] == second
    assert player.duration == pytest.approx(6.0, abs=0.05)
    await chain.wait_level(lambda rms: rms > LOUD, "ikinci şarkı duyulmuyor")
    assert len(chain.recorder.of("history")) == 1


@pytest.mark.browser
async def test_skip_pause_seek_resume_and_stop_drive_the_page_audio(chain):
    player, bot = chain.player, chain.bot
    await chain.join()
    for title in ("Uzun Şarkı", "Sonraki Şarkı", "Orta Şarkı"):
        await player.add(title, "Vedat")
    await chain.wait_playing("Uzun Şarkı")
    skipped = player._active_token
    await wait_until(lambda: player.position > 0.5, timeout=5, message="konum ilerlemedi")

    # ── Şarkının ortasında atla: tek ilerleme, eski jetonun "bitti"si yok sayılır ──
    await player.skip()
    await chain.wait_playing("Sonraki Şarkı")
    current = player._active_token
    assert current > skipped
    assert chain.titles(player.history) == ["Uzun Şarkı"] and chain.titles(player.queue) == ["Orta Şarkı"]
    assert (await chain.page_audio())["token"] == current
    await player.on_track_ended(skipped)   # geç gelen / bayat bitiş olayı
    await asyncio.sleep(0.3)
    assert player.current.title == "Sonraki Şarkı" and chain.titles(player.queue) == ["Orta Şarkı"]
    assert chain.ended == [] and len(bot.play_calls) == 2

    # ── Duraklat: sayfadaki ses durur, konum donar ────────────
    await wait_until(lambda: player.position > 1.0, timeout=5, message="konum ilerlemedi")
    await player.pause()
    assert player.state == "paused"
    audio = await chain.page_audio()
    assert audio["paused"] is True
    await chain.wait_level(lambda rms: rms < SILENT, "duraklatınca ses kesilmedi")
    assert (await chain.page_audio())["position"] == pytest.approx(audio["position"], abs=0.05)

    # ── Duraklatılmışken konum seç ────────────────────────────
    await player.seek(5.0)
    assert player.position == 5.0 and chain.recorder.last("progress")["position"] == 5.0
    assert (await chain.page_audio())["position"] == pytest.approx(5.0, abs=0.1)

    # ── Devam: 5. saniyeden çalmaya devam eder ────────────────
    await player.resume()
    assert player.state == "playing"
    await wait_until(lambda: player.position > 5.3, timeout=4, message="devam edince konum ilerlemedi")
    audio = await chain.page_audio()
    assert audio["paused"] is False and 5.0 < audio["position"] < 8.0
    await chain.wait_level(lambda rms: rms > LOUD, "devam edince ses gelmedi")

    # ── Durdur: ses ve ilerleme biter, kuyruk korunur ─────────
    await player.stop()
    assert player.state == "idle" and player.current is None
    assert (await chain.page_audio())["token"] is None
    await chain.wait_level(lambda rms: rms < SILENT, "durdurunca ses kesilmedi")
    count = len(chain.recorder.of("progress"))
    await asyncio.sleep(0.5)
    assert len(chain.recorder.of("progress")) == count
    assert chain.titles(player.history) == ["Sonraki Şarkı", "Uzun Şarkı"]
    assert chain.titles(player.queue) == ["Orta Şarkı"]
    assert chain.ended == []


@pytest.mark.browser
async def test_leave_pauses_and_rejoin_resumes_the_same_track_near_the_same_position(chain):
    player, bot = chain.player, chain.bot
    await chain.join()
    await player.add("Uzun Şarkı", "Vedat")
    await chain.wait_playing("Uzun Şarkı")
    first = player._active_token
    await wait_until(lambda: player.position >= 2.0, timeout=6, message="konum 2 sn'ye ulaşmadı")

    await bot.leave()
    await wait_until(lambda: player.state == "paused", timeout=5, message="ayrılınca duraklatılmadı")
    left_at = player.position
    assert 2.0 <= left_at < 6.0
    assert player.current.title == "Uzun Şarkı" and player.bot_status == "disconnected"
    assert chain.recorder.last("bot")["detail"] == "Toplantıdan ayrıldı"
    assert chain.recorder.last("playback")["state"] == "paused"
    assert chain.page.url == "about:blank"
    await asyncio.sleep(0.4)
    assert player.position == left_at   # ses gitti: konum donuk kalır, bitiş bildirilmez
    assert chain.ended == []

    await chain.join()
    await chain.wait_playing("Uzun Şarkı")
    assert player._active_token > first
    name, token, start_at = bot.play_calls[-1]
    assert (name, token) == ("vidUzun0001.wav", player._active_token)
    assert start_at == pytest.approx(left_at)
    audio = await chain.page_audio()
    assert left_at - 0.1 <= audio["position"] <= left_at + 2.5
    await chain.wait_level(lambda rms: rms > LOUD, "yeniden katılınca ses gelmedi")
    assert chain.ended == [] and list(player.history) == [] and len(bot.play_calls) == 2


@pytest.mark.browser
async def test_play_failure_marks_the_track_as_error_and_skips_to_the_next_with_a_notice(chain):
    player, bot = chain.player, chain.bot
    chain.downloader.corrupt.add("vidBozuk001")
    await player.add("Bozuk Şarkı", "Vedat")
    await player.add("Sonraki Şarkı", "Vedat")
    broken = player.queue[0]

    await chain.join()
    await chain.wait_playing("Sonraki Şarkı")
    assert broken.status == "error"
    assert [call[0] for call in bot.play_calls] == ["vidBozuk001.webm", "vidSonra001.wav"]
    warnings = [m["message"] for m in chain.recorder.of("notice") if m["level"] == "warning"]
    assert len(warnings) == 1 and warnings[0].startswith("⚠️ Bozuk Şarkı çalınamadı, atlandı: ")
    assert str(chain.settings.downloads_dir) not in warnings[0] and ".webm" not in warnings[0]   # yol sızmaz
    assert list(player.history) == [] and player.queue == []
    assert not (chain.settings.downloads_dir / "vidBozuk001.webm").exists()   # dosyası bırakıldı
    assert (await chain.page_audio())["token"] == player._active_token
    await chain.wait_level(lambda rms: rms > LOUD, "hatadan sonraki şarkı duyulmuyor")


# ──────────────────────────────────────────────────────────────
#  --fake-bot modu: WAV olmayan indirmelerin süresi
# ──────────────────────────────────────────────────────────────

@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe yok")
async def test_fake_bot_reports_the_real_duration_of_non_wav_downloads(settings, tmp_path, monkeypatch):
    files = {}
    for video_id, codec, ext, seconds in (("vidM4a00001", "aac", "m4a", 2.5), ("vidOpus0001", "libopus", "webm", 3.2)):
        path = tmp_path / f"{video_id}.{ext}"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                        "-c:a", codec, str(path)], check=True, timeout=60, capture_output=True)
        files[video_id] = path

    class FileDownloader(FakeDownloader):
        async def download(self, video_id: str) -> str:
            return str(files[video_id])

        def cleanup(self, path: str) -> None:
            self.cleaned.append(path)

    bot = FakeBot(settings, speed=0.001, tick=0.05, join_delay=0)
    downloader = FileDownloader(settings.downloads_dir)
    downloader.catalog["m4a şarkı"] = [make_info("vidM4a00001", "M4A Şarkı", None)]   # arama: süre bilinmiyor
    recorder = Recorder()
    player = Player(settings, bot, downloader, recorder)
    bot.on_status, bot.on_track_ended, bot.on_progress = player.on_bot_status, player.on_track_ended, player.on_progress
    try:
        bot.request_join(MEET_LINK)
        await wait_until(lambda: bot.is_connected)
        await player.add("m4a şarkı", "Vedat")
        await wait_until(lambda: player.state == "playing", timeout=10)
        assert player.duration == pytest.approx(2.5, abs=0.1)          # 30 sn değil
        assert recorder.last("playback")["duration"] == pytest.approx(2.5, abs=0.1)

        assert await bot.play(str(files["vidOpus0001"]), 99) == pytest.approx(3.2, abs=0.1)
        monkeypatch.setattr(fake_bot.shutil, "which", lambda name: None)   # ffprobe yok → varsayılan
        assert await bot.play(str(files["vidOpus0001"]), 100) == fake_bot.DEFAULT_DURATION
    finally:
        await player.shutdown()
        await bot.shutdown()
