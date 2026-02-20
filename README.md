# 🎵 MeetBot 4.0 — Google Meet Müzik Botu

MeetBot, Google Meet toplantılarına katılarak yüksek kaliteli ses paylaşımı yapan ve grup müzik deneyimi sunan modern bir bottur. Playwright tabanlı tarayıcı otomasyonu ve FastAPI tabanlı gerçek zamanlı web kontrol paneli (dashboard) ile donatılmıştır.

## 🚀 Öne Çıkan Özellikler

- **Modern Web Dashboard:** Kullanıcı dostu, karanlık mod destekli ve gerçek zamanlı (WebSocket) kontrol paneli.
- **Kolay Müzik Kuyruğu:** YouTube linklerini doğrudan yapıştırarak sıraya şarkı ekleme.
- **Gelişmiş Ses Kontrolü:** 
    - Müzik ve mikrofon için ayrı ses seviyesi ayarları.
    - "Gürültü Giderme" (Noise Cancellation) özelliğini otomatik olarak devre dışı bırakma (daha temiz müzik iletimi için).
- **Tam Denetim:** Oynat, Duraklat, Durdur, Geç ve Döngü (Loop) modları.
- **Web Audio API Enjeksiyonu:** Sesi doğrudan tarayıcı içerisinden, sistem sesini meşgul etmeden yüksek kalitede iletir.
- **Hızlı Kurulum:** Tek bir komutla ayağa kalkan sunucu ve bot yapısı.

## 🛠️ Kullanılan Teknolojiler

### **Backend (Arka Plan)**
- **Python & FastAPI:** Hızlı ve asenkron API/Sunucu altyapısı.
- **Playwright:** Google Meet etkileşimleri için tarayıcı otomasyonu.
- **yt-dlp:** YouTube videolarını indirmek ve ses formatına dönüştürmek için.
- **WebSockets:** Sunucu ve arayüz arasında anlık veri senkronizasyonu.

### **Frontend (Arayüz)**
- **Tailwind CSS:** Modern ve duyarlı (responsive) tasarım.
- **Vanilla JavaScript:** Framework bağımsız, hızlı ve hafif istemci mantığı.
- **FontAwesome:** Şık ikonlar.

## 📦 Kurulum ve Çalıştırma

### **1. Gereksinimler**
- **Python 3.9+**
- **Google Chrome** veya **Microsoft Edge** tarayıcısı.
- İnternet erişimi.

### **2. Bağımlılıkları Yükleyin**
Proje dizininde bir terminal açın ve gerekli kütüphaneleri yükleyin:
```bash
pip install -r requirements.txt
```

### **3. Browser Sürücülerini Yükleyin**
Playwright'ın tarayıcıları kontrol edebilmesi için:
```bash
playwright install chromium
```

### **4. Uygulamayı Başlatın**
```bash
python main.py
```

### **5. Kullanım**
- Tarayıcınızdan `http://localhost:8000` adresine gidin.
- Bir kullanıcı adı belirleyerek giriş yapın.
- Google Meet linkinizi "Katıl" bölümüne yapıştırın.
- Bot toplantıya katıldıktan sonra YouTube linklerini ekleyerek müzik keyfini başlatın!

## 📂 Dosya Yapısı

- `main.py`: Uygulamanın giriş noktası; sunucu ve botu başlatır.
- `server.py`: FastAPI sunucusu, API uç noktaları ve WebSocket yönetimi.
- `bot.py`: Playwright bot mantığı ve Web Audio API enjeksiyonu.
- `audio_manager.py`: Şarkı indirme ve dosya yönetimi işlemleri.
- `static/`: Web arayüzü dosyaları (HTML, CSS, JS).
- `requirements.txt`: Gerekli Python kütüphaneleri listesi.

## ⚠️ Önemli Notlar
- Botun Google Meet'e sorunsuz girebilmesi için Chrome profilinizin açık olması veya gerekli çerezlerin (`cookies`) ayarlanmış olması gerekebilir.
- Yüksek kaliteli ses için bot, Meet ayarlarındaki gürültü giderme özelliğini otomatik olarak kapatmaya çalışır.

---
*Geliştiren: [Vedat]*
