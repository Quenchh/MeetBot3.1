# ──────────────────────────────────────────────────────────────
#  server.py — FastAPI + WebSocket sunucusu
# ──────────────────────────────────────────────────────────────

import asyncio
import json
import os
import time
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from audio_manager import get_metadata, download_audio

from contextlib import asynccontextmanager

DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# ──────────────────────────────────────────────────────────────
#  Global State (RAM'de tutuluyor)
# ──────────────────────────────────────────────────────────────

cleanup_callback = None

def set_cleanup_callback(cb):
    """Shutdown sırasında çalışacak temizlik fonksiyonunu kaydet."""
    global cleanup_callback
    cleanup_callback = cb

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup işlemleri (varsa)
    print("🧹  Başlangıç temizliği yapılıyor (Downloads klasörü)...")
    try:
        if os.path.exists(DOWNLOADS_DIR):
            for filename in os.listdir(DOWNLOADS_DIR):
                file_path = os.path.join(DOWNLOADS_DIR, filename)
                if os.path.isfile(file_path):
                    os.remove(file_path)
    except Exception as e:
        print(f"⚠️  Başlangıç temizliği hatası: {e}")
        
    yield
    # Shutdown işlemleri
    if cleanup_callback:
        print("🛑  Sunucu kapanıyor (Lifespan)...")
        await cleanup_callback()

app_state = {
    "queue": [],              # [{id, title, duration_str, duration, url, added_by, added_at, file_path}]
    "current_song": None,     # Şu an çalan şarkı (queue item)
    "playback_state": "idle", # "playing", "paused", "idle"
    "loop": False,            # Döngü modu
    "music_volume": 80,       # Müzik ses seviyesi (0-100)
    "mic_volume": 80,         # Mikrofon çıkış sesi (0-100)
    "mic_muted": False,       # Mikrofon kapalı mı?
    "meet_link": None,        # Aktif Meet linki
    "bot_status": "disconnected",  # "disconnected", "connecting", "connected"
}

song_id_counter = 0

# Bağlı WebSocket istemcileri
connected_clients: List[WebSocket] = []

# Bot callback — bot.py tarafından set edilecek
bot_callback = None


def set_bot_callback(cb):
    """Bot'tan gelen callback fonksiyonunu kaydet."""
    global bot_callback
    bot_callback = cb


# ──────────────────────────────────────────────────────────────
#  FastAPI App
# ──────────────────────────────────────────────────────────────

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="MeetBot Müzik Sunucusu", lifespan=lifespan)

# CORS Middleware Ekle
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Güvenlik açısından üretimde kısıtlanmalı, ancak bot için * uygundur.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static dosyalar
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(STATIC_DIR, exist_ok=True)

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

class CustomHeaderMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "*"
        return response

app.add_middleware(CustomHeaderMiddleware)

app.mount("/downloads", StaticFiles(directory=DOWNLOADS_DIR), name="downloads")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def serve_index():
    """Ana sayfa — SPA"""
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ──────────────────────────────────────────────────────────────
#  Broadcast — tüm bağlı istemcilere mesaj gönder
# ──────────────────────────────────────────────────────────────

async def broadcast(message: dict):
    """Tüm bağlı WebSocket istemcilerine mesaj yayınla."""
    data = json.dumps(message, ensure_ascii=False)
    disconnected = []
    for ws in connected_clients:
        try:
            await ws.send_text(data)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in connected_clients:
            connected_clients.remove(ws)


def get_full_state() -> dict:
    """Güncel durumun tamamını döndür (yeni bağlanan için)."""
    return {
        "type": "state_sync",
        "queue": app_state["queue"],
        "current_song": app_state["current_song"],
        "playback_state": app_state["playback_state"],
        "loop": app_state["loop"],
        "music_volume": app_state["music_volume"],
        "mic_volume": app_state["mic_volume"],
        "mic_muted": app_state.get("mic_muted", False),
        "meet_link": app_state["meet_link"],
        "bot_status": app_state["bot_status"],
    }


# ──────────────────────────────────────────────────────────────
#  Şarkı yönetimi
# ──────────────────────────────────────────────────────────────

async def populate_song(song: dict):
    """Arka planda şarkıyı indir ve file_path'i güncelle."""
    if song.get("file_path") or song.get("_downloading"):
        return

    print(f"⬇️  Ön indirme başladı: {song['title']}")
    song["_downloading"] = True
    try:
        path = await download_audio(song["url"])
        song["file_path"] = path
        print(f"✅  Ön indirme tamam: {song['title']}")
        
        # Eğer indirilirken silindiyse dosyayı temizle
        if song.get("_removed"):
            print(f"🗑️  Şarkı indirme sırasında silinmiş, dosya temizleniyor: {song['title']}")
            cleanup_song(song)
    except Exception as e:
        print(f"⚠️  Ön indirme hatası ({song['title']}): {e}")
    finally:
        song["_downloading"] = False


def prefetch_next_songs():
    """Kuyruktaki sıradaki 2 şarkıyı önceden indir."""
    next_songs = app_state["queue"][:2]
    for song in next_songs:
        asyncio.create_task(populate_song(song))


def is_file_in_use(file_path: str, exclude_song_id: int) -> bool:
    """Belirtilen dosyanın kuyruktaki başka bir şarkı veya çalan şarkı tarafından kullanılıp kullanılmadığını kontrol eder."""
    if not file_path:
        return False
        
    if app_state["current_song"] and app_state["current_song"].get("id") != exclude_song_id:
        if app_state["current_song"].get("file_path") == file_path:
            return True
            
    for s in app_state["queue"]:
        if s.get("id") != exclude_song_id and s.get("file_path") == file_path:
            return True
            
    return False


def cleanup_song(song: dict):
    """Şarkı dosyasını sil (Eğer başka bir şarkı tarafından kullanılmıyorsa)."""
    if not song: return
    path = song.get("file_path")
    
    if path and os.path.exists(path):
        if is_file_in_use(path, song.get("id")):
            print(f"💡  Dosya silinmedi, kuyruktaki başka bir şarkı tarafından kullanılıyor: {song['title']}")
            return
            
        try:
            os.remove(path)
            print(f"🗑️  Dosya silindi: {song['title']}")
        except Exception as e:
            print(f"⚠️  Dosya silinemedi: {e}")


async def play_next(force_cleanup=False):
    """Kuyruktaki sıradaki şarkıyı çal."""
    
    # Eski şarkıyı temizle (Eğer loop kapalıysa veya force_cleanup açıksa)
    old_song = app_state["current_song"]
    if old_song:
        if force_cleanup or not app_state["loop"]:
            cleanup_song(old_song)
        # Looptaysa ve force_cleanup kapalıysa silme, tekrar oynatılacak

    if not app_state["queue"]:
        app_state["current_song"] = None
        app_state["playback_state"] = "idle"
        await broadcast({"type": "playback_update", **_playback_info()})
        return

    # Sıradakini al
    song = app_state["queue"].pop(0)
    app_state["current_song"] = song
    app_state["playback_state"] = "playing"

    # Ön indirmeyi tetikle (bir sonraki şarkılar için)
    prefetch_next_songs()

    # Şarkıyı indir (Eğer prefetch yetişmediyse burada bekler)
    try:
        if not song.get("file_path"):
            # Eğer şu an indiriliyorsa bekle (Maks 60sn)
            wait_counter = 0
            while song.get("_downloading") and wait_counter < 120:
                await asyncio.sleep(0.5)
                wait_counter += 1
            
            if wait_counter >= 120:
                print(f"⚠️  İndirme zaman aşımı (60sn): {song['title']}")
                song["_downloading"] = False
            
            # Hala yoksa indir
            if not song.get("file_path"):
                file_path = await download_audio(song["url"])
                song["file_path"] = file_path

        # Bot'a çal komutu gönder
        if bot_callback:
            filename = os.path.basename(song["file_path"])
            await bot_callback("play", {
                "url": f"/downloads/{filename}",
                "title": song["title"]
            })

    except Exception as e:
        print(f"⚠️  Şarkı indirme/çalma hatası: {e}")
        app_state["playback_state"] = "idle"
        # Hatalı şarkıyı atla
        await play_next()
        return

    await broadcast({
        "type": "playback_update",
        **_playback_info(),
    })
    await broadcast({"type": "queue_update", "queue": app_state["queue"]})


def _playback_info() -> dict:
    return {
        "current_song": app_state["current_song"],
        "playback_state": app_state["playback_state"],
        "loop": app_state["loop"],
    }


async def on_song_ended():
    """Şarkı bittiğinde çağrılır (bot tarafından)."""
    if app_state["loop"] and app_state["current_song"]:
        # Döngü modunda — aynı şarkıyı tekrar çal
        song = app_state["current_song"]
        if bot_callback:
            filename = os.path.basename(song["file_path"])
            await bot_callback("play", {
                "url": f"/downloads/{filename}",
                "title": song["title"],
            })
        return

    # Sonraki şarkıya geç
    await play_next()


# ──────────────────────────────────────────────────────────────
#  WebSocket Endpoint
# ──────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global song_id_counter

    await ws.accept()
    connected_clients.append(ws)
    print(f"🔌  Yeni WebSocket bağlantısı (toplam: {len(connected_clients)})")

    # Bağlanan kullanıcıya güncel durumu gönder
    # Bağlanan kullanıcıya güncel durumu gönder
    try:
        current_state = {
            "type": "state_sync",
            "queue": app_state["queue"],
            "current_song": app_state["current_song"],
            "playback_state": app_state["playback_state"],
            "loop": app_state["loop"],
            "music_volume": app_state["music_volume"],
            "mic_volume": app_state["mic_volume"],
            "mic_muted": app_state["mic_muted"],
            "bot_status": app_state["bot_status"],
            "meet_link": app_state["meet_link"],
        }
        await ws.send_text(json.dumps(current_state, ensure_ascii=False))
    except Exception as e:
        print(f"⚠️  İlk durum gönderilemedi: {e}")

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            # ── Şarkı ekleme ────────────────────────────────
            if msg_type == "add_song":
                url = msg.get("url", "").strip()
                added_by = msg.get("added_by", "Anonim")

                if not url:
                    await ws.send_text(json.dumps({
                        "type": "error", "message": "URL boş olamaz"
                    }))
                    continue

                print(f"🔍  Şarkı aranıyor: {url} (İsteyen: {added_by})")
                try:
                    metadata = await get_metadata(url)
                    print(f"✅  Metadata bulundu: {metadata['title']}")
                except Exception as e:
                    print(f"❌  Metadata hatası: {e}")
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "message": f"Şarkı bilgisi alınamadı: {str(e)}"
                    }))
                    continue

                song_id_counter += 1
                song = {
                    "id": song_id_counter,
                    "title": metadata["title"],
                    "duration": metadata["duration"],
                    "duration_str": metadata["duration_str"],
                    "url": url,
                    "added_by": added_by,
                    "added_at": datetime.now().strftime("%H:%M"),
                    "file_path": None,
                }
                app_state["queue"].append(song)
                print(f"➕  Kuyruğa eklendi: {song['title']}")

                await broadcast({
                    "type": "queue_update",
                    "queue": app_state["queue"],
                })
                await broadcast({
                    "type": "song_added",
                    "song": song,
                })

                # Eğer hiçbir şey çalmıyorsa, otomatik başlat
                if app_state["playback_state"] == "idle":
                    print("▶️  Otomatik oynatma başlatılıyor...")
                    asyncio.create_task(play_next())
                else:
                    # Sıradaki şarkıları kontrol et ve indir
                    print("⬇️  Arka planda indirme tetikleniyor...")
                    prefetch_next_songs()

            # ── Skip (Geç) ──────────────────────────────────
            elif msg_type == "skip":
                print("⏭️  Şarkı geçiliyor...")
                if bot_callback:
                    await bot_callback("stop", {})  # Bot'ta durdur ve sıfırla
                asyncio.create_task(play_next(force_cleanup=True))

            # ── Stop (Durdur/Reset) ─────────────────────────
            elif msg_type == "stop":
                print("⏹️  Müzik durduruldu (Reset).")
                if bot_callback:
                    await bot_callback("stop", {})
                
                # Çalan şarkıyı temizle
                if app_state["current_song"]:
                    cleanup_song(app_state["current_song"])
                
                app_state["playback_state"] = "idle"
                app_state["current_song"] = None
                await broadcast({"type": "playback_update", **_playback_info()})

            # ── Pause (Duraklat) ────────────────────────────
            elif msg_type == "pause":
                if app_state["playback_state"] == "playing":
                    print("⏸️  Müzik duraklatıldı.")
                    app_state["playback_state"] = "paused"
                    if bot_callback:
                        await bot_callback("pause", {})
                    await broadcast({"type": "playback_update", **_playback_info()})

            # ── Resume (Başlat/Devam Et) ────────────────────
            elif msg_type == "resume":
                if app_state["playback_state"] == "paused":
                    print("▶️  Müzik devam ettiriliyor...")
                    app_state["playback_state"] = "playing"
                    if bot_callback:
                        await bot_callback("resume", {})
                    await broadcast({"type": "playback_update", **_playback_info()})
                elif app_state["playback_state"] == "idle" and app_state["queue"]:
                    print("▶️  Kuyruktan oynatma başlatılıyor...")
                    asyncio.create_task(play_next())

            # ── Loop (Döngü) ────────────────────────────────
            elif msg_type == "loop":
                app_state["loop"] = not app_state["loop"]
                print(f"🔁  Döngü modu: {'Açık' if app_state['loop'] else 'Kapalı'}")
                await broadcast({"type": "playback_update", **_playback_info()})

            # ── Kuyruk Sıralama (Drag-and-Drop) ────────────
            elif msg_type == "reorder_queue":
                new_ids = msg.get("new_ids", [])
                if not new_ids:
                    continue

                print("list  Kuyruk yeniden sıralanıyor...")
                # Mevcut kuyruğu map'le
                current_queue_map = {item["id"]: item for item in app_state["queue"]}

                # Yeni sıralamayı oluştur
                new_queue = []
                for q_id in new_ids:
                    if q_id in current_queue_map:
                        new_queue.append(current_queue_map[q_id])

                # Listede olup da yeni sıralamada olmayanları (varsa) sona ekle
                for item in app_state["queue"]:
                    if item["id"] not in new_ids:
                        new_queue.append(item)

                app_state["queue"] = new_queue
                
                # Yeni sıralamaya göre ön indirme yap
                prefetch_next_songs()

                await broadcast({
                    "type": "queue_update",
                    "queue": app_state["queue"],
                })

            # ── Mikrofon Toggle (Aç/Kapa) ──────────────────
            elif msg_type == "toggle_mic":
                # Mevcut durumun tersine çevir
                current_mute = app_state.get("mic_muted", False)
                app_state["mic_muted"] = not current_mute
                
                print(f"🎤  Mikrofon durumu değiştirildi: {'KAPALI' if app_state['mic_muted'] else 'AÇIK'}")
                if bot_callback:
                    await bot_callback("set_mic_mute", {"muted": app_state["mic_muted"]})
                
                await broadcast({
                    "type": "mic_status",
                    "muted": app_state["mic_muted"]
                })

            # ── Ses seviyesi ────────────────────────────────
            elif msg_type == "set_volume":
                target = msg.get("target", "music")  # "music" veya "mic"
                value = max(0, min(100, int(msg.get("value", 80))))

                if target == "music":
                    app_state["music_volume"] = value
                    if bot_callback:
                        await bot_callback("set_music_volume", {"value": value})
                elif target == "mic":
                    app_state["mic_volume"] = value
                    if bot_callback:
                        await bot_callback("set_mic_volume", {"value": value})

                await broadcast({
                    "type": "volume_update",
                    "music_volume": app_state["music_volume"],
                    "mic_volume": app_state["mic_volume"],
                })

            # ── Meet'e katıl ───────────────────────────────
            elif msg_type == "join_meet":
                link = msg.get("link", "").strip()
                if not link:
                    await ws.send_text(json.dumps({
                        "type": "error", "message": "Meet linki boş olamaz"
                    }))
                    continue

                import re
                match = re.search(r"https://meet\.google\.com/[a-z0-9\-]+", link, re.IGNORECASE)
                if not match:
                    await ws.send_text(json.dumps({
                        "type": "error", "message": "Geçersiz Meet linki"
                    }))
                    continue
                
                link = match.group(0) # Fazlalıkları sil

                print(f"🔗  Meet bağlantı isteği: {link}")
                app_state["meet_link"] = link
                app_state["bot_status"] = "connecting"
                await broadcast({
                    "type": "bot_status",
                    "status": "connecting",
                    "meet_link": link,
                })

                # Bot'a katılma komutu gönder
                if bot_callback:
                    # Arka planda çalışması için task oluştur
                    asyncio.create_task(bot_callback("join_meet", {"link": link}))

            # ── Meet'ten ayrıl ─────────────────────────────
            elif msg_type == "leave_meet":
                print("👋  Meet'ten ayrılma isteği.")
                app_state["meet_link"] = None
                # Bot'a ayrıl komutu gönder
                if bot_callback:
                    asyncio.create_task(bot_callback("leave_meet", {}))
                else:
                    # Bot callback yoksa bile UI'ı güncelle
                    app_state["bot_status"] = "disconnected"
                    await broadcast({"type": "bot_status", "status": "disconnected", "meet_link": None})

            # ── Şarkı kaldır ───────────────────────────────
            elif msg_type == "remove_song":
                song_id = msg.get("id")
                
                # Silinecek şarkıyı bul ve temizle
                song_to_remove = next((s for s in app_state["queue"] if s["id"] == song_id), None)
                if song_to_remove:
                    song_to_remove["_removed"] = True
                    cleanup_song(song_to_remove)
                    
                original_len = len(app_state["queue"])
                app_state["queue"] = [s for s in app_state["queue"] if s["id"] != song_id]
                if len(app_state["queue"]) < original_len:
                     print(f"🗑️  Kuyruktan şarkı çıkarıldı (ID: {song_id})")

                await broadcast({
                    "type": "queue_update",
                    "queue": app_state["queue"],
                })

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"⚠️  WebSocket hatası: {e}")
    finally:
        if ws in connected_clients:
            connected_clients.remove(ws)
        print(f"🔌  WebSocket bağlantı koptu (kalan: {len(connected_clients)})")


# ──────────────────────────────────────────────────────────────
#  Bot durum güncellemesi (bot.py tarafından çağrılır)
# ──────────────────────────────────────────────────────────────

async def update_bot_status(status: str):
    """Bot durumunu güncelle ve broadcast et."""
    app_state["bot_status"] = status
    await broadcast({"type": "bot_status", "status": status, "meet_link": app_state["meet_link"]})


async def update_playback_progress(current: float, total: float):
    """Şarkı ilerlemesini broadcast et."""
    # Sadece playing durumundaysa gönder (gereksiz trafik olmasın)
    #if app_state["playback_state"] == "playing":
    await broadcast({
        "type": "progress_update",
        "current": current,
        "total": total
    })
