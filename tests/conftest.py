import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import load_settings  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    """İzole ayarlar: gerçek .env okunmaz, indirmeler geçici klasöre gider."""
    s = load_settings(
        env={
            "MEETBOT_ADMIN_PASSWORD": "test-secret",
            "MEETBOT_DOWNLOADS_DIR": str(tmp_path / "downloads"),
            "MEETBOT_PROFILE_DIR": str(tmp_path / "profile"),
        },
        dotenv_path=tmp_path / "missing.env",
    )
    s.downloads_dir.mkdir(parents=True, exist_ok=True)
    return s


# ──────────────────────────────────────────────────────────────
#  Tarayıcı testleri (bot): Playwright'ın Chromium'u + sahte medya cihazları
# ──────────────────────────────────────────────────────────────

import math  # noqa: E402
import struct  # noqa: E402
import wave  # noqa: E402

CHROMIUM_TEST_ARGS = [
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
]


@pytest.fixture
async def chromium():
    """Headless Chromium (sahte kamera/mikrofon). Başlatılamazsa test atlanır."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - playwright requirements'ta var
        pytest.skip(f"Playwright yok: {exc}")
    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.launch(headless=True, args=CHROMIUM_TEST_ARGS)
    except Exception as exc:
        await playwright.stop()
        pytest.skip(f"Chromium başlatılamadı: {exc}")
    try:
        yield browser
    finally:
        await browser.close()
        await playwright.stop()


def write_tone_wav(path, seconds=1.0, freq=440.0, rate=48_000, amplitude=0.5):
    """Mono 16 bit sinüs WAV dosyası yazar (tarayıcıda çalınacak test sesi)."""
    path = Path(path)
    peak = int(32767 * amplitude)
    frames = b"".join(
        struct.pack("<h", int(peak * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(int(seconds * rate))
    )
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        wav_file.writeframes(frames)
    return path


@pytest.fixture
def tone_wav(tmp_path):
    """tone_wav(seconds, freq=440) → geçici klasörde sinüs WAV dosyasının yolu."""
    counter = iter(range(1_000_000))

    def make(seconds=1.0, freq=440.0):
        return write_tone_wav(tmp_path / f"tone_{next(counter)}.wav", seconds, freq)

    return make
