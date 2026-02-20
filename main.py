# ──────────────────────────────────────────────────────────────
#  main.py — MeetBot Giriş Noktası
#  FastAPI sunucusunu ve Playwright botunu başlatır.
# ──────────────────────────────────────────────────────────────

import asyncio
import os
import sys
import threading
import uvicorn

from server import app, set_bot_callback, on_song_ended, set_cleanup_callback, update_playback_progress
from bot import MeetBot


# ──────────────────────────────────────────────────────────────
#  Global bot referansı
# ──────────────────────────────────────────────────────────────

bot = MeetBot()
bot_ready = False


async def bot_command_handler(command: str, data: dict):
    """
    Sunucudan bota gelen komutları yönlendirir.
    Bu fonksiyon server.py tarafından çağrılır.
    """
    global bot_ready

    if command == "join_meet":
        # Meet'e katılma işlemi — arka planda başlat
        asyncio.create_task(_join_meet_task(data["link"]))
    elif bot.page:  # bot_ready yerine bot.page varlığını kontrol et (daha esnek)
        try:
            await bot.handle_command(command, data)
        except Exception as e:
            print(f"⚠️  Komut işlenirken hata: {e}")
    else:
        print(f"⚠️  Bot henüz hazır değil, komut yoksayıldı: {command}")


async def _join_meet_task(link: str):
    """Meet'e katılma görevini arka planda çalıştır."""
    global bot_ready
    try:
        # Chrome'u başlat ve bağlan
        if not bot.browser:
            await bot.start_chrome()
            await bot.connect()

        # Şarkı bitti callback'i ayarla
        bot._on_song_ended = on_song_ended
        bot._on_progress = update_playback_progress

        # Meet'e katıl
        await bot.join_meet(link)
        bot_ready = True

        # Durumu güncelle
        from server import update_bot_status
        await update_bot_status("connected")
        print("\n🎉  Bot hazır! Web arayüzünden şarkı ekleyebilirsiniz.")

    except Exception as e:
        print(f"\n❌  Meet'e katılma hatası: {e}")
        from server import update_bot_status
        await update_bot_status("disconnected")


# ──────────────────────────────────────────────────────────────
#  Ana fonksiyon
# ──────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  🎵  MeetBot — Grup Müzik Botu v4.0")
    print("=" * 55)
    print()
    print("  📡  Sunucu başlatılıyor...")
    print("  🌐  Arayüz: http://localhost:8000")
    print("  📋  Meet linkini web arayüzünden girin.")
    print()
    print("=" * 55)

    # Bot callback'ini sunucuya kaydet
    set_bot_callback(bot_command_handler)

    # Shutdown temizliği için callback'i sunucuya bildir
    set_cleanup_callback(bot.cleanup)

    # Uvicorn sunucusunu başlat

    # Uvicorn sunucusunu başlat
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="warning",
    )
    server = uvicorn.Server(config)

    # Windows ProactorLoop bug fix (WinError 10054 output suppression)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def loop_exception_handler(loop, context):
        # Yoksayılacak hatalar
        exception = context.get("exception")
        if exception and isinstance(exception, ConnectionResetError):
            return
        if "WinError 10054" in str(context.get("message", "")) or "WinError 10054" in str(exception):
            return
        
        # Diğer hataları normal şekilde bas
        loop.default_exception_handler(context)

    loop.set_exception_handler(loop_exception_handler)

    try:
        loop.run_until_complete(server.serve())
    except KeyboardInterrupt:
        print("\n\n👋  Sunucu kapatılıyor...")
    # Finally bloğuna gerek yok, shutdown event halleder.


if __name__ == "__main__":
    main()
