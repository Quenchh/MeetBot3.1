<div align="center">

# 🎵 MeetBot 4.0 
**Google Meet Müzik ve Ses Botu**

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/release/python-390/)
[![FastAPI](https://img.shields.io/badge/FastAPI-005571?style=flat&logo=fastapi)](https://fastapi.tiangolo.com/)
[![Playwright](https://img.shields.io/badge/Playwright-2EAD33?style=flat&logo=playwright&logoColor=white)](https://playwright.dev/)
[![TailwindCSS](https://img.shields.io/badge/Tailwind_CSS-38B2AC?style=flat&logo=tailwind-css&logoColor=white)](https://tailwindcss.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

*Google Meet toplantılarına katılıp doğrudan tarayıcı içinden, mikrofonu meşgul etmeden, yüksek kaliteli stüdyo sesiyle müzik paylaşımı yapan modern, asenkron ve otonom bir bot.*

---

</div>

## 🚀 Öne Çıkan Özellikler

- 🎛️ **Modern Web Dashboard:** Neon Synthwave/Vaporwave estetiğine sahip, duyarlı ve gerçek zamanlı (WebSocket tabanlı) kontrol paneli.
- 🎶 **Sonsuz Müzik Kuyruğu:** Sürükle-bırak desteğiyle YouTube linklerini kuyruğa ekleme ve sıralama. Arka planda `yt-dlp` ile anında indirme ve önbellekleme (prefetch).
- 🔊 **Gelişmiş Ses Enjeksiyonu (Web Audio API):** Sesi sanal kabloya veya sisteme ihtiyaç duymadan, doğrudan tarayıcının ses devresine *48kHz* kalitesinde aktarır. 
- 🎚️ **Bağımsız Ses Kontrolü:** Odaya giden *Müzik* ve *Mikrofon* ses düzeylerini ayrı ayrı ayarlayabilme.
- 🤖 **Tam Otonom Katılım:** Meet linkini girdiğiniz an otonom olarak (Playwright & CSS Selector) toplantıya katılır, kamerasını kapatır ve gürültü gidermeyi (Noise Cancellation) müziği bozmaması için otomatik deaktive eder.
- 🔄 **Oynatma Modları:** Oynat, Duraklat, Durdur, Geç (Skip) ve Döngü (Loop) seçenekleri.
- 🔐 **Admin Yetkilendirmesi:** Panelin müzik dinleyicileri tarafından görüntülenip, sadece şifreli giriş yapan adminler tarafından kontrol edilmesi.
- 🛡️ **Hata Toleransı:** Otomatik çökme (Aw, Snap!) kurtarması, çift URL engellemesi ve kopan WebSocket bağlantılarını anında onarma.

---

## 🛠️ Kullanılan Teknolojiler

### **Backend (Arka Plan)**
- **Python & FastAPI:** Asenkron, hızlı ve hafif web sunucusu.
- **Playwright (async):** Başsız (Headless) veya görünür konfigürasyonla Google Chrome otomasyonu.
- **yt-dlp:** Hızlı veri çekimi ve ses dosyası dönüşümü.
- **WebSockets:** İstemci ile sunucu arasında milisaniyelik gecikmeyle (zero-lag) haberleşme.

### **Frontend (Arayüz)**
- **Tailwind CSS:** Esnek ve anında stilize edilebilir yapı. UI/UX odaklı animasyonlar ve neon efektler.
- **Vanilla JavaScript:** 0 bağımlılık, `app.js` üzerinden yönetilen DOM manipülasyonu.
- **Google Material Symbols:** Estetik ve ölçeklenebilir ikon ailesi.

---

## 📦 Kurulum ve Çalıştırma

### **1. Sistem Gereksinimleri**
- **Python 3.9** veya daha güncel bir sürüm.
- **Google Chrome** (veya Microsoft Edge) sisteminizde kurulu olmalıdır.
- (İsteğe bağlı) Ses dönüşümleri için bilgisayarınızda `ffmpeg` bulunması tavsiye edilir.

### **2. Bağımlılıkları Yükleyin**
Projeyi klonladıktan veya indirdikten sonra terminali proje dizininde açın:
```bash
pip install -r requirements.txt
```

### **3. Playwright Tarayıcılarını Hazırlayın**
Playwright'ın bağımsız olarak sekme yönetebilmesi için Chromium ortamını indirin:
```bash
playwright install chromium
```

### **4. Uygulamayı Başlatın**
```bash
python main.py
```
> Sunucu `http://127.0.0.1:8000` veya bulunduğunuz ağın yerel IP'si üzerinden yayına başlar.

---

## 🎮 Kullanım Rehberi

1. **Dashboard'a Erişim:** Tarayıcınızdan `http://localhost:8000` adresine gidin.
2. **Kullanıcı Adı:** Sisteme bağlandığınızda sizi temsil edecek bir isim belirleyin.
3. **Toplantıya Katılım (Admin):** 
   - Sağ üstteki kilit ikonuna basıp admin şifresi (`xasimaymun123` - *kod içerisinden değiştirilebilir*) ile yetki alın.
   - Google Meet linkinizi panoya yapıştırıp **Katıl** butonuna basın. Bot arka planda odaya girecektir.
4. **Müzik Ekleme:** YouTube linkinizi yapıştırın. Bot anında şarkı bilgilerini çekip kuyruğa dahil edecektir.
5. **Kontrol:** Parçaları sıraya dizebilir, sürükleyebilir, durdurabilir ve ince ses ayarlarını panelden canlı olarak yapabilirsiniz!

---

## 📂 Dosya ve Mimari Yapısı

```bash
📦 MeetBot3.0
 ┣ 📂 chrome_profil/    # Otomasyon için kalıcı çerez ve oturum dosyaları
 ┣ 📂 downloads/        # İndirilen ve geçici olarak çalınan müzik dosyaları
 ┣ 📂 static/           # Frontend (HTML, CSS, JS) kaynakları
 ┃ ┣ 📜 app.js
 ┃ ┣ 📜 index.html
 ┃ ┗ 📜 styles.css
 ┣ 📜 audio_manager.py  # yt-dlp ile müzik indirme / kuyruk algoritması
 ┣ 📜 bot.py            # Playwright işlemleri, Web Audio JS Injection, Seçiciler
 ┣ 📜 main.py           # Sunucu ayağa kaldırma, Uvicorn tetikleyicisi
 ┣ 📜 server.py         # FastAPI rotaları, WebSocket haberleşmesi, State yönetimi
 ┣ 📜 create_silence.py # Sahte mikrofon beslemesi için boş ses üreteci
 ┗ 📜 requirements.txt  # Python paket bağımlılıkları
```

---

## ⚠️ Önemli Notlar ve Sorun Giderme

- **Sandbox Hataları (Linux/Sunucu):** Eğer sunucuda çalıştırıyorsanız `bot.py` içindeki Chrome başlatma argümanlarında `--no-sandbox` bulunduğundan emin olun.
- **Toplantıya Kabul:** Bot toplantıya kendi Google hesabı olmadan "Anonim" olarak katılır (Hesap girilmediyse). Meet sahibinin botu **kabul etmesi** gerekmektedir.
- **Çift URL Hatası:** Kullanıcı kaynaklı çift URL kopyalama (`https://meet.google.com/xxxhttps://...`) gibi syntax hataları frontend ve backend filtreleriyle otomatik temizlenir.
- **Ses Kalitesi:** Meet'in kendi arayüzünde "Gürültü Giderme" aktif olursa müzik seste bozulmalara yol açabilir. Bot bunu *kendi kendine* kapatacak şekilde dizayn edilmiştir.

---

<div align="center">
  <p>🎨 <b>Vedat</b> tarafından sevgiyle geliştirildi.</p>
</div>
