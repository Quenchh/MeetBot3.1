# ──────────────────────────────────────────────────────────────
#  bot.py — Playwright Bot + Web Audio API Injection
# ──────────────────────────────────────────────────────────────

import asyncio
import os
import sys
import subprocess
import socket
import platform

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ──────────────────────────────────────────────────────────────
#  Sabitler
# ──────────────────────────────────────────────────────────────

CDP_PORT = 9222
PROFIL_DIZINI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome_profil")
SAYFA_YUKLEME_MS = 30_000
KATILIM_BEKLEME_MS = 120_000  # 2 dakika

# ──────────────────────────────────────────────────────────────
#  Web Audio API Enjeksiyon Scripti
# ──────────────────────────────────────────────────────────────

AUDIO_INJECT_SCRIPT = """
(() => {
    console.log("[MeetBot] Init Script Başlatıldı.");

    if (window.__meetbot_injected) return;
    window.__meetbot_injected = true;

    // 1. Audio Engine Kurulumu (48kHz - Meet Standardı)
    const ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 48000 });
    const dest = ctx.createMediaStreamDestination();
    
    const musicGain = ctx.createGain();
    const micGain = ctx.createGain();
    musicGain.gain.value = 0.8;
    micGain.gain.value = 0.8;

    // Zincir: Music -> Mic -> Destination
    musicGain.connect(micGain);
    micGain.connect(dest);

    // Bot Kontrol Nesnesi
    window.__meetbot = {
        ctx, dest, musicGain, micGain,
        audio: null, source: null, isPlaying: false,
        
        async play(url) {
            console.log("[MeetBot] Çalma isteği:", url);
            
            // Temizlik: Eskiyi durdur ve kopar
            if (this.audio) {
                this.audio.pause();
                this.audio.src = "";
                this.audio.load();
                this.audio = null;
            }
            if (this.source) {
                try { this.source.disconnect(); } catch(e) {}
                this.source = null;
            }

            if (ctx.state === 'suspended') await ctx.resume();

            // Yeni Audio Elementi (Her şarkı için taze başlangıç)
            const audio = new Audio();
            audio.crossOrigin = "anonymous";
            audio.src = url;

            // canplaythrough bekleyerek senkronizasyon sağla (30sn Timeout)
            await new Promise((resolve, reject) => {
                const timeout = setTimeout(() => {
                    cleanup();
                    reject(new Error("Audio yükleme zaman aşımı (30sn): " + url));
                }, 30000);

                const cleanup = () => {
                    clearTimeout(timeout);
                    audio.removeEventListener("canplaythrough", onCanPlay);
                    audio.removeEventListener("error", onError);
                };

                const onCanPlay = () => {
                   cleanup();
                   resolve();
                };
                const onError = (e) => {
                   cleanup();
                   reject(new Error("Audio yükleme hatası: " + url));
                };
                
                audio.addEventListener("canplaythrough", onCanPlay);
                audio.addEventListener("error", onError);
                audio.load();
            });

            const source = ctx.createMediaElementSource(audio);
            source.connect(musicGain);

            this.audio = audio;
            this.source = source;
            this.isPlaying = true;

            audio.onended = () => {
                this.isPlaying = false;
                window.__meetbot_song_ended = true;
            };

            await audio.play();
            console.log("[MeetBot] Başladı: 48kHz engine aktif.");
        },

        stop() {
            if (this.audio) { this.audio.pause(); this.audio.currentTime = 0; }
            this.isPlaying = false;
        },

        pause() { if (this.audio) this.audio.pause(); },
        resume() { if (this.audio) this.audio.play(); },
        setMusicVolume(v) { musicGain.gain.setTargetAtTime(v/100, ctx.currentTime, 0.01); },
        setMicVolume(v) { micGain.gain.setTargetAtTime(v/100, ctx.currentTime, 0.01); }
    };

    // 2. getUserMedia Patch (DefineProperty ile sarsılmaz hale getir)
    const origGUM = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    const patchGUM = async function(constraints) {
        if (constraints && constraints.audio) {
            console.log("[MeetBot] 🎤 Mikrofon isteği yakalandı, müzik hattına bağlandı.");
            if (ctx.state === 'suspended') await ctx.resume();
            
            if (constraints.video) {
                try {
                    const vidStream = await origGUM({ ...constraints, audio: false });
                    const mixed = new MediaStream();
                    vidStream.getVideoTracks().forEach(t => mixed.addTrack(t));
                    dest.stream.getAudioTracks().forEach(t => mixed.addTrack(t));
                    return mixed;
                } catch(e) {
                    return dest.stream;
                }
            }
            return dest.stream;
        }
        return origGUM(constraints);
    };

    Object.defineProperty(navigator.mediaDevices, 'getUserMedia', {
        value: patchGUM,
        writable: true,
        configurable: true
    });

    if (navigator.getUserMedia) {
        navigator.getUserMedia = patchGUM;
    }

    console.log("[MeetBot] Patch tamamlandı.");
})();
"""


# ──────────────────────────────────────────────────────────────
#  Chrome yolu ve port kontrolü
# ──────────────────────────────────────────────────────────────

def chrome_yolunu_bul() -> str:
    """Sistemde kurulu Chrome veya Edge'in yolunu bulur."""
    if platform.system() == "Windows":
        adaylar = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        ]
    elif platform.system() == "Darwin":
        adaylar = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    else:
        adaylar = ["/usr/bin/google-chrome", "/usr/bin/chromium-browser", "/usr/bin/microsoft-edge"]

    for yol in adaylar:
        if os.path.exists(yol):
            return yol

    raise FileNotFoundError("Chrome veya Edge bulunamadı!")


def port_kullaniliyormu(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


# ──────────────────────────────────────────────────────────────
#  MeetBot sınıfı
# ──────────────────────────────────────────────────────────────

class MeetBot:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.chrome_process = None
        self._song_ended_check_task = None
        self._on_song_ended = None  # Callback
        self._on_progress = None    # Callback (current, total)

    async def start_chrome(self):
        """Chrome'u debug modunda başlat."""
        chrome_yolu = chrome_yolunu_bul()
        os.makedirs(PROFIL_DIZINI, exist_ok=True)

        if port_kullaniliyormu(CDP_PORT):
            print(f"⚠️  Port {CDP_PORT} zaten kullanımda, mevcut Chrome'a bağlanılıyor...")
            return

        silence_wav = os.path.abspath(os.path.join(os.path.dirname(__file__), "silence.wav"))
        if not os.path.exists(silence_wav):
            print(f"⚠️  UYARI: silence.wav bulunamadı! ({silence_wav})")
            # Dosya yoksa oluşturmayı dene
            try:
                import wave
                with wave.open(silence_wav, "w") as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(44100)
                    wav_file.writeframes(b"\x00" * 44100 * 2)
                print(f"✅  silence.wav oluşturuldu.")
            except Exception as e:
                print(f"❌  silence.wav oluşturulamadı: {e}")

        args = [
            chrome_yolu,
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={PROFIL_DIZINI}",
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",  # Sahte cihaz (Dosya ile beslenecek)
            f"--use-file-for-fake-audio-capture={silence_wav}", # Beep yerine SESSİZLİK dosyasını kullan!
            "--allow-file-access-from-files",
            "--disable-infobars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-notifications",
            "--autoplay-policy=no-user-gesture-required",
            "--headless=new",
            "about:blank",
        ]

        # Windows'ta pencereyi gizleme (isteğe bağlı, şimdilik açık kalsın debug için)
        self.chrome_process = subprocess.Popen(
            args,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )

        # Chrome'un başlamasını bekle
        for _ in range(30):
            if port_kullaniliyormu(CDP_PORT):
                break
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError("Chrome başlatılamadı!")

        print("✅  Chrome hazır.")

    async def connect(self):
        """Playwright ile Chrome'a CDP üzerinden bağlan."""
        self.playwright = await async_playwright().start()
        print(f"🔗  Chrome'a CDP bağlantısı kuruluyor...")
        self.browser = await self.playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{CDP_PORT}"
        )
        self.context = self.browser.contexts[0]

        # Mevcut sayfaları kontrol et
        pages = self.context.pages
        if pages:
            self.page = pages[0]
        else:
            self.page = await self.context.new_page()

        # Stealth uygula
        try:
            from playwright_stealth import Stealth
            await Stealth().apply_stealth_async(self.page)
        except Exception as e:
            print(f"⚠️  Stealth uygulanamadı: {e}")

        # Web Audio API enjeksiyonunu init script olarak ekle
        # Bu, sayfa yüklenmeden önce çalışır ve getUserMedia'yı patch'ler.
        # Böylece Meet mikrofon istediğinde bizim sahte stream'imizi alır.
        await self.context.add_init_script(AUDIO_INJECT_SCRIPT)
        print("✅  Audio Injection Script (Init) eklendi.")
        await self.page.add_init_script(AUDIO_INJECT_SCRIPT)

        print("✅  Playwright bağlantısı kuruldu.")

    async def join_meet(self, link: str):
        """Google Meet toplantısına katıl."""
        print(f"🌐  Meet'e gidiliyor: {link}")
        await self.page.goto(link, wait_until="domcontentloaded", timeout=SAYFA_YUKLEME_MS)

        # Sayfanın yüklenmesini bekle
        await self.page.wait_for_timeout(6000)

        # Audio inject script'i çalıştır (sayfa yüklendikten sonra)
        try:
            await self.page.evaluate(AUDIO_INJECT_SCRIPT)
        except Exception:
            pass

        # Katılma butonunu bul ve tıkla
        print("🔘  Katılma butonu aranıyor...")
        buton_metinleri = [
            "Hemen katıl", "Hemen Katıl",
            "Katılma isteği gönder",
            "Ask to join", "Şimdi katıl",
            "Join now", "Katıl", "Join",
        ]

        buton_tiklandi = False
        for metin in buton_metinleri:
            try:
                buton = self.page.get_by_role("button", name=metin)
                if await buton.is_visible(timeout=3000):
                    await buton.click()
                    buton_tiklandi = True
                    print(f'✅  "{metin}" butonuna tıklandı.')
                    break
            except Exception:
                continue

        if not buton_tiklandi:
            try:
                fallback = self.page.locator(
                    "button:has-text('join'), button:has-text('katıl'), "
                    "button:has-text('Join'), button:has-text('Katıl'), "
                    "button:has-text('Hemen')"
                ).first
                await fallback.click(timeout=5000)
                buton_tiklandi = True
                print("✅  Katılma butonuna (yedek seçici) tıklandı.")
            except Exception:
                pass

        if not buton_tiklandi:
            raise RuntimeError("Katılma butonu bulunamadı!")

        # Toplantıya kabul edilmeyi bekle
        print(f"⏳  Toplantıya kabul bekleniyor (maks {KATILIM_BEKLEME_MS // 1000} sn)...")

        toplanti_ici_seciciler = [
            '[aria-label="Görüşmeden ayrıl"]',
            '[aria-label="Leave call"]',
            '[data-tooltip="Görüşmeden ayrıl"]',
            '[data-tooltip="Leave call"]',
            '[aria-label*="ikrofon"]',
            '[aria-label*="icrophone"]',
            '[aria-label*="amera"]',
            '[aria-label="Diğer seçenekler"]',
            '[aria-label="More options"]',
        ]
        birlesik_secici = ", ".join(toplanti_ici_seciciler)

        try:
            await self.page.wait_for_selector(birlesik_secici, timeout=KATILIM_BEKLEME_MS)
            print("✅  Bot toplantıya başarıyla katıldı!")
        except PlaywrightTimeout:
            raise RuntimeError("Zaman aşımı! Toplantı sahibi onay vermedi.")

        # Toplantı içi: SADECE kamerayı kapat (Mikrofon açık kalmalı)
        await self.page.wait_for_timeout(2000)
        await self._kamera_kapat()

        # Audio inject'i tekrar çalıştır (Meet sayfasında)
        try:
            await self.page.evaluate(AUDIO_INJECT_SCRIPT)
        except Exception:
            pass

        # Gürültü gidermeyi kapat
        await self.page.wait_for_timeout(1000)
        await self._gurultu_giderme_kapat()

        # Şarkı bitti kontrolünü başlat
        self._start_song_ended_checker()

    async def _kamera_kapat(self):
        """Sadece kamerayı kapat (Mikrofon AÇIK kalmalı ki müzik gitsin)."""
        # Kamera
        cam_seciciler = [
            '[aria-label*="amerayı kapat"]',
            '[aria-label="Turn off camera"]',
            '[data-tooltip*="amerayı kapat"]',
        ]
        for s in cam_seciciler:
            try:
                btn = self.page.locator(s).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    print("📷  Kamera kapatıldı.")
                    break
            except Exception:
                continue

    async def _gurultu_giderme_kapat(self):
        """Diğer seçenekler → Ayarlar → Gürültü giderme toggleını kapat."""
        print("⚙️  Gürültü giderme ayarı kontrol ediliyor...")
        
        # 1. "Diğer seçenekler" menüsünü aç
        # Daha spesifik ve hızlı seçiciler
        uc_nokta_seciciler = [
            '[aria-label="Diğer seçenekler"]', 
            '[aria-label="More options"]', 
            'button i:has-text("more_vert")', # İkon tabanlı (bazen işe yarar)
            'button:has(i.google-material-icons:has-text("more_vert"))',
        ]
        
        menu_acildi = False
        for s in uc_nokta_seciciler:
            try:
                # Timeout'u düşürdük, hızlı denesin
                btn = self.page.locator(s).first
                if await btn.is_visible(timeout=1000):
                    await btn.click()
                    menu_acildi = True
                    break
            except Exception:
                continue
        
        if not menu_acildi:
            # Fallback: Kaba kuvvet arama (ikon ismi vs)
            try:
                await self.page.locator("button").filter(has_text="more_vert").first.click(timeout=1000)
                menu_acildi = True
            except:
                print("⚠️  'Diğer seçenekler' butonu bulunamadı, ayarlar atlanıyor.")
                return

        await self.page.wait_for_timeout(500)

        # 2. "Ayarlar"a tıkla
        ayarlar_acildi = False
        try:
            # Role tabanlı arama daha güvenilir
            settings_item = self.page.get_by_role("menuitem", name="Ayarlar").or_(
                            self.page.get_by_role("menuitem", name="Settings"))
            
            if await settings_item.is_visible(timeout=2000):
                await settings_item.click()
                ayarlar_acildi = True
        except Exception:
            pass
        
        if not ayarlar_acildi:
            # Metin tabanlı fallback
            try:
                await self.page.get_by_text("Ayarlar", exact=True).click(timeout=1000)
                ayarlar_acildi = True
            except:
                try:
                    await self.page.get_by_text("Settings", exact=True).click(timeout=1000)
                    ayarlar_acildi = True
                except:
                    # Menüyü kapatmak için Esc
                    await self.page.keyboard.press("Escape")
                    print("⚠️  'Ayarlar' menüsü bulunamadı.")
                    return

        await self.page.wait_for_timeout(1000)

        # 3. Gürültü giderme switchini kapat (Robust JS Mantığı)
        print("🔍  Gürültü giderme toggle'ı aranıyor (JS)...")
        try:
            # Önce "Ses/Audio" sekmesine geçildiğinden emin ol
            await self.page.evaluate('''() => {
                const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
                const audioTab = tabs.find(t => t.innerText.includes("Ses") || t.innerText.includes("Audio"));
                if (audioTab) audioTab.click();
            }''')
            await self.page.wait_for_timeout(500)

            result = await self.page.evaluate('''() => {
                const toggles = Array.from(document.querySelectorAll('[role="switch"], [role="checkbox"]'));
                for (const t of toggles) {
                    // Toggle'ın üst elementlerinde "gürültü" veya "noise" ara
                    let p = t.parentElement;
                    for (let i = 0; i < 5 && p; i++) {
                        if (p.innerText.includes("Gürültü giderme") || p.innerText.includes("Noise cancellation")) {
                            const isChecked = t.getAttribute("aria-checked") === "true" || t.checked;
                            if (isChecked) {
                                t.click();
                                return "KAPATILDI";
                            }
                            return "ZATEN_KAPALI";
                        }
                        p = p.parentElement;
                    }
                }
                return "BULUNAMADI";
            }''')
            print(f"📊  Gürültü giderme sonucu: {result}")
        except Exception as e:
            print(f"⚠️  JS Gürültü giderme hatası: {e}")

        # 4. Ayarlar penceresini kapat (KESİN)
        print("✖️  Ayarlar kapatılıyor...")
        await self.page.wait_for_timeout(500)
        
        close_strategies = [
            lambda: self.page.get_by_label("Kapat").click(timeout=1000),
            lambda: self.page.get_by_label("Close").click(timeout=1000),
            lambda: self.page.keyboard.press("Escape"),
        ]

        for strategy in close_strategies:
            try:
                await strategy()
                await self.page.wait_for_timeout(300)
            except:
                continue
        
        # Kesin kapandığından emin olmak için bir ESC daha
        await self.page.keyboard.press("Escape")

        # 5. Diğer olası pop-up'ları kapat (örn: "İzin ver" vs)
        await self._close_generic_popups()

    async def _close_generic_popups(self):
        """Ekranda kalmış olabilecek genel uyarı/bilgi pop-up'larını kapatır."""
        try:
            # Yaygın kapatma butonları
            popups = [
                '[aria-label="Kapat"]', '[aria-label="Close"]',
                'button:has-text("Anladım")', 'button:has-text("Got it")',
                'button:has-text("Hayır")', 'button:has-text("No thanks")',
                '[data-mdc-dialog-action="close"]'
            ]
            for selector in popups:
                try:
                    element = self.page.locator(selector).first
                    if await element.is_visible(timeout=500):
                        await element.click()
                        print(f"🧹  Pop-up kapatıldı: {selector}")
                except:
                    pass
        except:
            pass

    # ── Bot komutları (sunucudan gelir) ─────────────────────

    async def play_audio(self, url: str):
        """Belirtilen URL'deki ses dosyasını çal."""
        try:
            await self.page.evaluate(AUDIO_INJECT_SCRIPT)
        except Exception:
            pass

        try:
            # URL'yi tam URL'ye çevir
            full_url = f"http://localhost:8000{url}" if url.startswith("/") else url
            await self.page.evaluate(f'window.__meetbot && window.__meetbot.play("{full_url}")')
            print(f"▶️  Çalınıyor: {url}")
        except Exception as e:
            print(f"⚠️  Ses çalma hatası: {e}")

    async def stop_audio(self):
        """Sesi durdur (reset)."""
        try:
            await self.page.evaluate('window.__meetbot && window.__meetbot.stop()')
            print("⏹️  Ses durduruldu (Reset).")
        except Exception as e:
            print(f"⚠️  Ses durdurma hatası: {e}")

    async def pause_audio(self):
        """Sesi duraklat."""
        try:
            await self.page.evaluate('window.__meetbot && window.__meetbot.pause()')
            print("⏸️  Ses duraklatıldı.")
        except Exception as e:
            print(f"⚠️  Ses duraklatma hatası: {e}")

    async def resume_audio(self):
        """Sesi devam ettir."""
        try:
            await self.page.evaluate('window.__meetbot && window.__meetbot.resume()')
            print("▶️  Ses devam ettiriliyor.")
        except Exception as e:
            print(f"⚠️  Ses devam ettirme hatası: {e}")

    async def set_music_volume(self, value: int):
        """Müzik ses seviyesini ayarla (0-100)."""
        try:
            await self.page.evaluate(f'window.__meetbot && window.__meetbot.setMusicVolume({value})')
        except Exception:
            pass

    async def set_mic_volume(self, value: int):
        """Mikrofon çıkış ses seviyesini ayarla (0-100)."""
        try:
            await self.page.evaluate(f'window.__meetbot && window.__meetbot.setMicVolume({value})')
        except Exception:
            pass

    async def set_meet_mic_mute(self, muted: bool):
        """Meet arayüzündeki mikrofon butonunu kullanarak mute/unmute yap."""
        print(f"🎤  Mikrofon durumu ayarlanıyor: {'Kapalı' if muted else 'Açık'}")
        
        # Tek seferde tüm alternatifleri ara (Hızlandırma)
        try:
            if muted:
                # Kapatma butonları
                selector = (
                    '[aria-label*="ikrofonu kapat"], '
                    '[aria-label="Turn off microphone"], '
                    '[data-tooltip*="ikrofonu kapat"]'
                )
                action = "kapatıldı"
            else:
                # Açma butonları
                selector = (
                    '[aria-label*="ikrofonu aç"], '
                    '[aria-label="Turn on microphone"], '
                    '[data-tooltip*="ikrofonu aç"]'
                )
                action = "açıldı"

            # Bekleme süresini düşürdük (zaten görünürse tıklar)
            btn = self.page.locator(selector).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                print(f"✅  Mikrofon {action}.")
            else:
                print("ℹ️  Mikrofon zaten istenen durumda.")

        except Exception as e:
            pass  # Hata bastır, akışı bozma

    def _start_song_ended_checker(self):
        """Şarkı bittiğini ve ilerlemeyi periyodik kontrol et."""
        if self._song_ended_check_task:
            self._song_ended_check_task.cancel()

        self._last_progress_update = 0

        async def check_loop():
            while True:
                await asyncio.sleep(1)
                try:
                    # Hem bitti mi diye bak, hem de süreleri al
                    data = await self.page.evaluate('''() => {
                        const ended = window.__meetbot_song_ended;
                        if (ended) window.__meetbot_song_ended = false;
                        
                        let current = 0;
                        let total = 0;
                        if (window.__meetbot && window.__meetbot.audio) {
                            current = window.__meetbot.audio.currentTime || 0;
                            total = window.__meetbot.audio.duration || 0;
                        }
                        
                        return { ended: !!ended, current, total };
                    }''')
                    
                    # 1. Şarkı bitti mi?
                    if data["ended"] and self._on_song_ended:
                        await self._on_song_ended()
                    
                    # 2. İlerleme güncellemesi (Sadece oynatılıyorsa ve anlamlı fark varsa)
                    # Her saniye gönderiyoruz
                    if self._on_progress and data["total"] > 0:
                        await self._on_progress(data["current"], data["total"])

                except Exception:
                    pass

        self._song_ended_check_task = asyncio.create_task(check_loop())

    async def handle_command(self, command: str, data: dict):
        """Sunucudan gelen komutu işle."""
        if command == "play":
            await self.play_audio(data["url"])
        elif command == "stop":
            await self.stop_audio()
        elif command == "pause":
            await self.pause_audio()
        elif command == "resume":
            await self.resume_audio()
        elif command == "set_music_volume":
            await self.set_music_volume(data["value"])
        elif command == "set_mic_volume":
            await self.set_mic_volume(data["value"])
        elif command == "set_mic_mute":
            await self.set_meet_mic_mute(data["muted"])
        elif command == "join_meet":
            try:
                await self.join_meet(data["link"])
                # Başarılı — durumu güncelle
                from server import update_bot_status
                await update_bot_status("connected")
            except Exception as e:
                print(f"❌  Meet'e katılma hatası: {e}")
                from server import update_bot_status
                await update_bot_status("disconnected")
        
        elif command == "leave_meet":
            await self.leave_meet()
            from server import update_bot_status
            await update_bot_status("disconnected")

    async def leave_meet(self):
        """Toplantıdan ayrıl."""
        if not self.page:
            return

        print("👋  Toplantıdan ayrılınıyor...")
        
        # Ayrıl butonuna tıkla
        try:
            leave_btn = self.page.locator('[aria-label="Görüşmeden ayrıl"], [aria-label="Leave call"]').first
            if await leave_btn.is_visible(timeout=2000):
                await leave_btn.click()
                print("✅  Ayrıl butonuna tıklandı.")
            else:
                print("⚠️  Ayrıl butonu bulunamadı, direkt sayfayı kapatıyorum.")
        except Exception as e:
            print(f"⚠️  Ayrılma hatası: {e}")

        # Her durumda ana sayfaya dön veya boş sayfaya git
        try:
            await self.page.goto("about:blank")
        except:
            pass

    async def cleanup(self):
        """Temizlik."""
        if self._song_ended_check_task:
            self._song_ended_check_task.cancel()
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.playwright:
            await self.playwright.stop()
        if self.chrome_process:
            self.chrome_process.terminate()
