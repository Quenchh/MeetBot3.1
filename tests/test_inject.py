# ──────────────────────────────────────────────────────────────
#  meetbot_inject.js — gerçek (headless) Chromium'da ses motoru testleri
#
#  Sayfa https://meetbot.test/ adresinde route ile sunulur (güvenli bağlam →
#  navigator.mediaDevices var). Sahte kamera/mikrofon Chromium bayraklarıyla
#  gelir; müziğin gerçekten getUserMedia izine aktığı AnalyserNode ile ölçülür.
# ──────────────────────────────────────────────────────────────

import asyncio
import base64

import pytest
from playwright.async_api import Error as PlaywrightError

import bot
from bot import audio_mime_type, invoke, load_inject_script, upload_audio

pytestmark = pytest.mark.browser

ORIGIN = "https://meetbot.test"
PAGE_HTML = """<!doctype html><html><body>
<p>engine test</p>
<iframe srcdoc="<p>child frame</p>"></iframe>
</body></html>"""

# Ayrı bir AudioContext'te getUserMedia izini dinleyen ölçer (Meet'in yerine).
LISTEN_JS = """async (constraints) => {
    const stream = await navigator.mediaDevices.getUserMedia(constraints || { audio: true });
    const ctx = new AudioContext();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 2048;
    ctx.createMediaStreamSource(stream).connect(analyser);
    window.__probe = { stream, analyser };
    return stream.getTracks().map((t) => t.kind).sort();
}"""

LEVEL_JS = """async (ms) => {
    await new Promise((resolve) => setTimeout(resolve, ms));
    const analyser = window.__probe.analyser;
    const data = new Float32Array(analyser.fftSize);
    analyser.getFloatTimeDomainData(data);
    return Math.sqrt(data.reduce((sum, x) => sum + x * x, 0) / data.length);
}"""


@pytest.fixture
async def page(chromium):
    context = await chromium.new_context()

    async def serve(route):
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=PAGE_HTML)

    await context.route(f"{ORIGIN}/**", serve)
    page = await context.new_page()
    await page.add_init_script(script=load_inject_script())
    await page.goto(f"{ORIGIN}/")
    yield page
    await context.close()


async def upload(page, path, token):
    return await upload_audio(page, path.read_bytes(), audio_mime_type(path), token)


async def load_and_play(page, path, token, start_at=0.0):
    url = await upload(page, path, token)
    return url, await invoke(page, "play", token, url, start_at)


async def status(page):
    return await invoke(page, "status")


async def level(page, ms=400):
    return await page.evaluate(LEVEL_JS, ms)


async def wait_until(predicate, timeout=5.0, interval=0.05):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("koşul zamanında sağlanmadı")


async def blob_revoked(page, url):
    return await page.evaluate(
        "async (u) => { try { await fetch(u); return false; } catch (e) { return true; } }", url)


# ── Enjeksiyon ────────────────────────────────────────────────

async def test_injection_is_idempotent_top_frame_only_and_engine_is_lazy(page):
    assert await page.evaluate("() => window.__meetbot.engine") is None  # AudioContext henüz yok
    await page.evaluate("() => { window.__first = window.__meetbot; }")
    await page.evaluate(load_inject_script())  # bot.py her gezinmeden sonra tekrar çalıştırır
    assert await page.evaluate("() => window.__first === window.__meetbot")

    child = next(frame for frame in page.frames if frame is not page.main_frame)
    assert await child.evaluate("() => typeof window.__meetbot") == "undefined"
    assert await child.evaluate("() => window.__meetbot_injected === undefined")


# ── getUserMedia yaması ───────────────────────────────────────

async def test_gum_audio_returns_independent_clones_of_the_music_bus(page, tone_wav):
    info = await page.evaluate("""async () => {
        const a = await navigator.mediaDevices.getUserMedia({ audio: true });
        const b = await navigator.mediaDevices.getUserMedia({ audio: { deviceId: "başka-mikrofon" } });
        const { dest, ctx } = window.__meetbot.engine;
        const destTrack = dest.stream.getAudioTracks()[0];
        const [ta, tb] = [a.getAudioTracks()[0], b.getAudioTracks()[0]];
        ta.stop();  // Meet eski bir akışı bırakınca...
        window.__keep = b;
        return {
            ids: new Set([ta.id, tb.id, destTrack.id]).size,
            counts: [a.getTracks().length, b.getTracks().length],
            afterStop: [tb.readyState, destTrack.readyState],
            sampleRate: ctx.sampleRate,
        };
    }""")
    assert info["ids"] == 3            # herkese kendi izi, hiçbiri hedefin kendisi değil
    assert info["counts"] == [1, 1]
    assert info["afterStop"] == ["live", "live"]  # ...bot susmaz
    assert info["sampleRate"] == 48000

    await page.evaluate("""() => {
        const ctx = new AudioContext();
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 2048;
        ctx.createMediaStreamSource(window.__keep).connect(analyser);
        window.__probe = { stream: window.__keep, analyser };
    }""")
    assert await level(page, 200) < 0.01          # çalan bir şey yokken sessiz
    await load_and_play(page, tone_wav(3.0), token=1)
    assert await level(page) > 0.05               # müzik, Meet'in aldığı ize akıyor


async def test_gum_audio_video_keeps_the_camera_and_video_only_passes_through(page):
    kinds = await page.evaluate("""async () => {
        const s = await navigator.mediaDevices.getUserMedia({ audio: true, video: true });
        const audio = s.getAudioTracks()[0];
        const result = {
            kinds: s.getTracks().map((t) => t.kind).sort(),
            fromBus: window.__meetbot.engine.dest.stream.getAudioTracks()[0].label === audio.label,
        };
        s.getTracks().forEach((t) => t.stop());
        return result;
    }""")
    assert kinds == {"kinds": ["audio", "video"], "fromBus": True}

    video_only = await page.evaluate("""async () => {
        const s = await navigator.mediaDevices.getUserMedia({ video: true });
        const kinds = s.getTracks().map((t) => t.kind);
        s.getTracks().forEach((t) => t.stop());
        return kinds;
    }""")
    assert video_only == ["video"]


# ── Aktarım + çalma ───────────────────────────────────────────

async def test_blob_upload_and_play_reports_duration_and_progress(page, tone_wav, monkeypatch):
    monkeypatch.setattr(bot, "UPLOAD_CHUNK_BYTES", 32 * 1024)  # ~190 KB → birden çok parça
    wav = tone_wav(2.0)
    url = await upload(page, wav, token=1)
    assert url.startswith(f"blob:{ORIGIN}/")

    result = await invoke(page, "play", 1, url, 0)
    assert result["duration"] == pytest.approx(2.0, abs=0.05)
    assert result["paused"] is False

    first = await status(page)
    await asyncio.sleep(0.5)
    second = await status(page)
    assert second["token"] == 1
    assert second["duration"] == pytest.approx(2.0, abs=0.05)
    assert second["position"] > first["position"] + 0.2
    assert (second["paused"], second["ended"], second["loading"], second["error"]) == (False, False, False, None)


async def test_ended_is_per_token_and_never_reported_for_stale_tokens(page, tone_wav):
    short, long = tone_wav(0.4), tone_wav(3.0)

    await load_and_play(page, short, token=1)
    await wait_until(lambda: _ended(page), timeout=5)
    assert (await status(page))["token"] == 1

    # stop(): parça tamamen gider, token 2 için asla "ended" gelmez
    await load_and_play(page, long, token=2)
    await asyncio.sleep(0.2)
    await invoke(page, "stop")
    assert (await status(page))["token"] is None

    # Yeniden çalma: token 3 bitmeden token 4 başlar → 3'ün bitişi hiç görünmez
    await load_and_play(page, short, token=3)
    await load_and_play(page, long, token=4)
    await asyncio.sleep(0.8)  # token 3 hâlâ yaşasaydı çoktan biterdi
    current = await status(page)
    assert current["token"] == 4
    assert current["ended"] is False
    assert current["position"] > 0.3


async def _ended(page):
    return (await status(page))["ended"]


async def test_overlapping_play_supersedes_the_older_one(page, tone_wav):
    url6 = await upload(page, tone_wav(3.0, freq=660), token=6)
    # İkinci bir blob URL'si (aktarım dışı): iki play() aynı anda yüklenirken çakışsın.
    url5 = await page.evaluate("async (u) => URL.createObjectURL(await (await fetch(u)).blob())", url6)
    outcome = await page.evaluate("""async ([u5, u6]) => {
        const api = window.__meetbot;
        const first = api.play(5, u5, 0);
        const second = api.play(6, u6, 0);
        const [a, b] = await Promise.allSettled([first, second]);
        return { a: a.status, reason: a.reason ? a.reason.message : null, b: b.status, token: api.status().token };
    }""", [url5, url6])
    assert outcome["a"] == "rejected"
    assert outcome["reason"].startswith("MEETBOT_SUPERSEDED")
    assert outcome["b"] == "fulfilled"
    assert outcome["token"] == 6
    assert await blob_revoked(page, url5)       # eski parçanın blob'u serbest
    assert not await blob_revoked(page, url6)


async def test_unclaimed_upload_is_revoked_and_can_not_be_played_later(page, tone_wav):
    # Aktarım bitti ama play() gelmeden stop(): blob bırakılır, geç gelen play() çalmaz.
    url1 = await upload(page, tone_wav(1.0), token=1)
    await invoke(page, "stop")
    assert await blob_revoked(page, url1)
    with pytest.raises(PlaywrightError, match="MEETBOT_SUPERSEDED"):
        await invoke(page, "play", 1, url1, 0)
    assert (await status(page))["token"] is None

    # Yeni aktarım, sahiplenilmemiş öncekini bırakır; sahiplenilen blob ise parçayla yaşar.
    url2 = await upload(page, tone_wav(1.0), token=2)
    url3 = await upload(page, tone_wav(1.0), token=3)
    assert await blob_revoked(page, url2)
    with pytest.raises(PlaywrightError, match="MEETBOT_SUPERSEDED"):
        await invoke(page, "play", 2, url2, 0)
    await invoke(page, "play", 3, url3, 0)
    await invoke(page, "uploadBegin", 4, "audio/wav")  # çalan parçanın blob'una dokunmaz
    assert not await blob_revoked(page, url3)
    assert (await status(page))["token"] == 3
    await invoke(page, "stop")
    assert await blob_revoked(page, url3)


async def test_start_offset_seek_pause_and_resume(page, tone_wav):
    await load_and_play(page, tone_wav(5.0), token=1, start_at=2.0)
    assert (await status(page))["position"] >= 2.0

    assert await invoke(page, "seek", 3.5) == pytest.approx(3.5)
    assert (await status(page))["position"] >= 3.4

    await invoke(page, "pause")
    paused = await status(page)
    await asyncio.sleep(0.4)
    still = await status(page)
    assert paused["paused"] and still["paused"]
    assert still["position"] == pytest.approx(paused["position"], abs=0.01)

    await invoke(page, "resume")
    await asyncio.sleep(0.4)
    resumed = await status(page)
    assert resumed["paused"] is False
    assert resumed["position"] > still["position"] + 0.2

    # Sonun ötesine atlama, parçanın bitişinin hemen önüne sıkıştırılır
    assert await invoke(page, "seek", 100) == pytest.approx(4.75)


async def test_pause_while_loading_starts_the_track_paused(page, tone_wav):
    url = await upload(page, tone_wav(2.0), token=1)
    result, state = await page.evaluate("""async (url) => {
        const api = window.__meetbot;
        const pending = api.play(1, url, 0);
        api.pause();
        const result = await pending;
        await new Promise((resolve) => setTimeout(resolve, 300));
        return [result, api.status()];
    }""", url)
    assert result["paused"] is True
    assert state["token"] == 1 and state["paused"] is True and state["loading"] is False
    assert state["position"] == 0

    await invoke(page, "resume")
    await asyncio.sleep(0.3)
    assert (await status(page))["position"] > 0.1


async def test_music_and_mic_volume_scale_the_output_and_normalize_compresses(page, tone_wav):
    assert await page.evaluate(LISTEN_JS, {"audio": True}) == ["audio"]
    await invoke(page, "setNormalize", False)
    await invoke(page, "setMusicVolume", 100)
    await invoke(page, "setMicVolume", 100)
    await load_and_play(page, tone_wav(6.0), token=1)

    full = await level(page)
    assert full > 0.2  # 0.5 genlikli sinüs ≈ 0.35 RMS

    await invoke(page, "setMusicVolume", 25)
    assert await level(page) / full == pytest.approx(0.25, abs=0.07)

    await invoke(page, "setMicVolume", 50)
    assert await level(page) / full == pytest.approx(0.125, abs=0.05)

    await invoke(page, "setMusicVolume", 100)
    await invoke(page, "setMicVolume", 100)
    assert await page.evaluate("() => window.__meetbot.engine.compressor.reduction") == 0  # zincir dışında
    await invoke(page, "setNormalize", True)
    assert await level(page) > 0.1  # ses kesilmedi (kompresör kendi telafi kazancını uygular)
    # Kompresör artık zincirde ve yüksek sesi bastırıyor
    assert await page.evaluate("() => window.__meetbot.engine.compressor.reduction") < -1

    await invoke(page, "setMusicVolume", 0)
    assert await level(page) < 0.005
    assert await page.evaluate("() => window.__meetbot.engine.musicGain.gain.value") == pytest.approx(0, abs=0.001)


# ── Temizlik ve hatalar ───────────────────────────────────────

async def test_stop_releases_the_blob_and_aborts_an_upload(page, tone_wav):
    url, _ = await load_and_play(page, tone_wav(2.0), token=1)
    await invoke(page, "stop")
    assert (await status(page))["token"] is None
    assert await blob_revoked(page, url)

    await invoke(page, "uploadBegin", 2, "audio/wav")
    await invoke(page, "stop")
    chunk = base64.b64encode(b"\x00" * 16).decode()
    with pytest.raises(PlaywrightError, match="MEETBOT_SUPERSEDED"):
        await invoke(page, "uploadChunk", 2, chunk)


async def test_upload_with_a_stale_token_is_rejected(page):
    await invoke(page, "uploadBegin", 1, "audio/wav")
    await invoke(page, "uploadBegin", 2, "audio/wav")
    chunk = base64.b64encode(b"\x00" * 16).decode()
    with pytest.raises(PlaywrightError, match="MEETBOT_SUPERSEDED"):
        await invoke(page, "uploadChunk", 1, chunk)
    with pytest.raises(PlaywrightError, match="MEETBOT_EMPTY"):
        await invoke(page, "uploadEnd", 2)


async def test_undecodable_file_reports_load_error_and_leaves_nothing_loaded(page):
    url = await upload_audio(page, b"bu bir ses dosyasi degil " * 200, "audio/wav", 1)
    with pytest.raises(PlaywrightError, match="MEETBOT_LOAD_ERROR"):
        await invoke(page, "play", 1, url, 0)
    assert (await status(page))["token"] is None
    assert await blob_revoked(page, url)


async def test_commands_without_a_track(page):
    with pytest.raises(PlaywrightError, match="MEETBOT_NO_TRACK"):
        await invoke(page, "pause")
    with pytest.raises(PlaywrightError, match="MEETBOT_NO_TRACK"):
        await invoke(page, "seek", 3)
    await invoke(page, "stop")  # durdurulacak bir şey yoksa sessizce geçer
    assert await status(page) == {
        "token": None, "position": 0, "duration": 0, "paused": True, "ended": False, "error": None, "loading": False,
    }
