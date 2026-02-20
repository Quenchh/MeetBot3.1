# ──────────────────────────────────────────────────────────────
#  audio_manager.py — yt-dlp ile YouTube ses yönetimi
# ──────────────────────────────────────────────────────────────

import asyncio
import os
import json
import hashlib
import time

DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)


async def get_metadata(url: str) -> dict:
    """
    YouTube linkinden şarkı adı ve süresini çeker (indirmeden).
    Dönen dict: {"title": str, "duration": int (saniye), "duration_str": str}
    """
    cmd = [
        "yt-dlp",
        "--no-download",
        "--print", "%(title)s",
        "--print", "%(duration)s",
        "--no-warnings",
        "--no-playlist",
        "--encoding", "utf-8",
        url,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp metadata hatası: {stderr.decode('utf-8', errors='replace').strip()}")

    lines = stdout.decode('utf-8', errors='ignore').strip().split("\n")
    if len(lines) < 2:
        raise RuntimeError("yt-dlp beklenmeyen çıktı formatı")

    title = lines[0].strip()
    try:
        duration = int(float(lines[1].strip()))
    except ValueError:
        duration = 0

    # Süreyi mm:ss formatına çevir
    mins, secs = divmod(duration, 60)
    duration_str = f"{mins}:{secs:02d}"

    return {
        "title": title,
        "duration": duration,
        "duration_str": duration_str,
    }


async def download_audio(url: str) -> str:
    """
    YouTube linkinden sesi indirir, opus formatında kaydeder.
    Dönen değer: dosya yolu (str)
    """
    # URL'den benzersiz dosya adı oluştur
    url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
    output_template = os.path.join(DOWNLOADS_DIR, f"{url_hash}.%(ext)s")

    # Daha önce indirilmiş mi kontrol et
    for ext in ["mp3", "m4a", "opus", "webm"]:
        candidate = os.path.join(DOWNLOADS_DIR, f"{url_hash}.{ext}")
        if os.path.exists(candidate):
            return candidate

    cmd = [
        "yt-dlp",
        "-x",                          # Sadece ses
        "--audio-format", "mp3",        # MP3 formatı (evrensel uyumluluk)
        "--audio-quality", "0",         # En iyi kalite
        "-o", output_template,
        "--no-playlist",
        "--no-warnings",
        url,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp indirme hatası: {stderr.decode('utf-8', errors='replace').strip()}")

    # İndirilen dosyayı bul
    for ext in ["mp3", "m4a", "opus", "webm", "ogg"]:
        candidate = os.path.join(DOWNLOADS_DIR, f"{url_hash}.{ext}")
        if os.path.exists(candidate):
            return candidate

    raise RuntimeError("İndirme tamamlandı ama dosya bulunamadı")
