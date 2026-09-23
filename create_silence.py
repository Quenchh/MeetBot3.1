# ──────────────────────────────────────────────────────────────
#  create_silence.py — Sessiz WAV üreteci
#
#  Chrome'un sahte mikrofonu (--use-file-for-fake-audio-capture) bu
#  dosyayı çalar. getUserMedia yaması çalıştığı sürece Meet bu sesi hiç
#  almaz; dosya yalnızca yamanın devreye girmediği durumlar için bir
#  güvenlik ağıdır (bip sesi yerine sessizlik). bot.py dosya yoksa
#  otomatik oluşturur; elle çalıştırmak isteğe bağlıdır:
#      python create_silence.py
# ──────────────────────────────────────────────────────────────

from __future__ import annotations

import wave
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent / "silence.wav"
SAMPLE_RATE = 48_000  # Web Audio motoru ve Meet ile aynı örnekleme hızı


def create_silence_wav(path: str | Path = DEFAULT_PATH, duration: float = 1.0,
                       sample_rate: int = SAMPLE_RATE) -> Path:
    """Mono, 16 bit, `duration` saniyelik sessiz bir WAV dosyası yazar ve yolunu döndürür."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame_count = max(1, int(sample_rate * duration))
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * frame_count)
    return path


if __name__ == "__main__":
    print(f"✅ {create_silence_wav()} oluşturuldu")
