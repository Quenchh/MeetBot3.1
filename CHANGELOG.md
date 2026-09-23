# 📜 Değişiklik Günlüğü

MeetBot'taki önemli değişiklikler bu dosyada tutulur.
Biçim [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) önerisine dayanır; sürüm numaraları [Semantic Versioning](https://semver.org/) kurallarını izler.

## [3.2.0] - 2026-09-23

3.1'in kapsamlı bir denetiminden sonra büyük ölçüde yeniden yazılan sürüm. Sunucu, oynatıcı, bot ve arayüz yeni bir mimariye ve WebSocket protokolüne (v2) geçti.
> ⚠️ **Uyumsuz değişiklikler:**
> - WebSocket protokolü değişti (bkz. [docs/PROTOCOL.md](docs/PROTOCOL.md)).
> - En az **Python 3.10** gerekiyor.
> - Yönetici şifresi artık `.env` ya da ortam değişkeniyle veriliyor.

### Güvenlik
- **Yönetici yetkisi yalnızca istemcideydi.** Şifre (`password123`) `app.js` içine gömülüydü; sunucu, paneli açan herkesten gelen her komutu uyguluyordu. Artık:
  - Şifre sunucuda `hmac.compare_digest` ile doğrulanıyor.
  - Başarılı girişte `secrets.token_urlsafe` ile bellekte tutulan bir oturum anahtarı üretiliyor.
  - 5 hatalı denemeden sonra bağlantı 60 sn kilitleniyor.
  - **Her mesajda** yetki denetleniyor (misafir / kontrol / yönetici).
  - Şifre `MEETBOT_ADMIN_PASSWORD` ile veriliyor. Boş bırakılırsa her açılışta rastgele üretilip konsola yazılıyor.
- **yt-dlp argüman enjeksiyonu.** Kullanıcının yazdığı metin yt-dlp'ye doğrudan konumsal argüman olarak veriliyordu; `--exec …` gibi bir değer seçenek olarak yorumlanabiliyordu. Artık:
  - Her çağrıda `--ignore-config` kullanılıyor ve tek konumsal argümandan önce `--` geliyor.
  - yt-dlp'ye yalnızca doğrulanmış kimliklerden yeniden kurulan kanonik YouTube adresleri ya da `ytsearch1:` sorguları gidiyor.
- **yt-dlp üzerinden SSRF / iç ağ taraması.** Her adres (localhost, CDP portu, `169.254.169.254`, `file://`…) yt-dlp'ye veriliyor ve hata çıktısı istemciye yansıtılıyordu. Artık:
  - Yalnızca `MEETBOT_ALLOWED_HOSTS` (YouTube) alan adları kabul ediliyor; diğerleri süreç başlatılmadan reddediliyor.
  - İstemciler kısa Türkçe mesajlar görüyor; ayrıntılar yalnızca sunucu günlüğüne yazılıyor.
- **Şarkı başlığıyla kalıcı XSS.** `escapeHtml` tırnakları kaçırmadığından başlık `title="…"` özniteliğinden taşabiliyordu. Arayüz artık her şeyi DOM API'leri ve `textContent` ile kuruyor:
  - `innerHTML`, `insertAdjacentHTML`, `eval` gibi yapılar test ile yasaklandı.
  - Resim ve bağlantı adresleri yalnızca `https` olabiliyor.
- **Siteler arası WebSocket ele geçirme.** `/ws` her Origin'den bağlantı kabul ediyordu ve CORS joker (`*`) açıktı. Artık Origin'in `host:port` kısmı `Host` ile eşleşmezse bağlantı reddediliyor (1008 / HTTP 403). CORS ara katmanları ve `/downloads` kaldırıldı.
- **`taskkill /F /IM chrome.exe` bilgisayardaki TÜM Chrome'ları kapatıyordu** (kullanıcının kendi tarayıcısı dahil). Bu, kimliği doğrulanmamış bir `join_meet` hatasıyla bile tetiklenebiliyordu. Artık:
  - Yalnızca botun başlattığı süreç ağacı sonlandırılıyor (`taskkill /PID <pid> /T /F`); isimle öldürme yok.
  - Botun başlatmadığı bir tarayıcı asla kapatılmıyor.
- Chrome artık `--allow-file-access-from-files` ile açılmıyor. `--no-sandbox` yalnızca Linux'ta root olarak çalışırken ekleniyor.
- İstemcilere artık mutlak dosya yolları (Windows kullanıcı adı dahil) ve iç alanlar gönderilmiyor; yalnızca `Track.public()` beyaz listesi gidiyor.
- **Sınırsız kaynak kullanımı giderildi:**
  - Kuyruk, kişi başı ve şarkı süresi sınırları; oynatma listesi sınırı.
  - Canlı yayınlar reddediliyor.
  - yt-dlp zaman aşımları var ve zaman aşımında süreç ağacı öldürülüyor. Aynı anda en fazla 3 çözümleme ve 2 indirme yapılıyor.
  - Mesaj boyutu (16 KB) ve hız sınırı (30 mesaj / 10 sn), bağlantı başına 3 eşzamanlı ekleme, en fazla 100 bağlantı.
- **Son adversaryal inceleme turunda giderilenler:**
  - Ayrıştırıcıyı şaşırtan adreslerle (ör. `youtube.com` gibi görünen ama `\`, boşluk ya da kontrol karakteri içeren host'lar) yt-dlp'nin YouTube dışı bir sunucuya bağlanması (SSRF). Host adları artık katı bir kalıpla denetleniyor, adresler sabit bir YouTube host'u üzerinde yeniden kuruluyor ve yt-dlp'nin genel çıkarıcısı kapatıldı (`--use-extractors default,-generic`).
  - Tek bir istemci 100 bağlantı sınırını doldurup herkesi dışarıda bırakabiliyordu. Artık aynı adresten en fazla 10 bağlantı var ve 10 sn içinde `hello` göndermeyen bağlantı kapatılıyor.
  - Yeniden bağlanarak saniyede ~135 şifre denenebiliyordu. Kilit artık **adres başına** (5 hata → 60 sn, her kilitte iki katı, en fazla 1 saat) ve toplamda dakikada 30 hatalı deneme sınırı var. Kısa bir elle verilmiş şifre açılışta uyarı veriyor.
  - Hız sınırı işi azaltmıyordu: büyük ya da fazla çerçeveler yine işleniyordu. Artık her çerçeve ayrıştırılmadan önce sayılıyor, 64 KB üstü çerçeveler bağlantıyı kapatıyor (1009) ve 10 sn'de 60'tan fazla çerçeve gönderen bağlantı kapatılıyor (1008). Kalp atışı pingleri hız sınırından muaf.
  - DNS-rebinding: `Host` başlığı artık denetleniyor (IP, tek parçalı ad, `.local`/`.lan` gibi yerel adlar ya da yeni `MEETBOT_TRUSTED_HOSTNAMES` ayarındaki adlar).
  - Açılıştaki temizlik indirme klasöründeki **bütün** dosyaları siliyordu; artık yalnızca MeetBot'un kendi indirmelerini siliyor.

### Düzeltmeler
- **Chrome 153 ile hiç ses çalmıyordu.** Chrome'un *Local Network Access* koruması, `https://meet.google.com` sayfasının `http://localhost:8000/downloads/…` adresinden ses yüklemesini engelliyordu. Artık:
  - Bot dosyayı diskten okuyup CDP üzerinden parça parça sayfaya yüklüyor ve bir `Blob` adresinden çalıyor.
  - Sunucu dosyaları HTTP ile yayınlamıyor.
- **Anonim katılım çalışmıyordu:** Görünen ad hiç yazılmadığından *Katılma isteği gönder* düğmesi pasif kalıyordu. Artık ad `MEETBOT_BOT_NAME` ile dolduruluyor ve düğmeye yalnızca etkinleşince basılıyor (TR + EN).
- **"Katıldı" algılaması yanlıştı.** Mikrofon, kamera ve *Diğer seçenekler* düğmeleri katılma ve bekleme ekranında da bulunduğu için kabul, ret ve zaman aşımı hiç algılanmıyordu. Artık:
  - Yalnızca *Görüşmeden ayrıl / Leave call* düğmesi toplantıda olmak sayılıyor.
  - Ret, yanıtsız istek, zaman aşımı, geçersiz kod ve başlamamış toplantı Türkçe açıklamalarla ayırt ediliyor.
- Playwright'ın `is_visible(timeout=…)` çağrısı zaman aşımını yok sayıp hemen döndüğünden kontroller Meet'in çizimiyle yarışıyordu. Bunların yerine bekleme ve yoklama kullanılıyor.
- **`play_next` yarış koşulları.** Aşağıdaki sorunlar `asyncio.Lock`, çalma başına artan jeton ve nesil sayacı kullanan yeni `Player` durum makinesiyle giderildi:
  - İndirme sürerken durdur, geç ya da duraklat yine de eski şarkıyı çalıyordu.
  - Aynı anda gelen geç + bitti iki şarkı atlatabiliyordu.
  - Üst üste gelen `play()` çağrıları iki şarkıyı mikrofonda karıştırıyordu.
- **Hiçbir şey çalmazken durum "çalıyor" görünüyordu** (bot toplantıda değilken ya da çalma hataları yutulduğunda). Artık:
  - `playing` durumu yalnızca bot çalmayı gerçekten başlattıktan sonra ayarlanıyor.
  - Çalma hataları bildiriliyor ve şarkı atlanıyor.
  - Bot toplantıdan düşerse şarkı duraklıyor ve bot geri gelince kaldığı yerden devam ediyor.
- Bir indirme hatası, özyinelemeli `play_next` yüzünden **bütün kuyruğu siliyordu**. Artık hatalı şarkı işaretlenip bir uyarıyla atlanıyor.
- yt-dlp'nin zaman aşımı yoktu. Bir canlı yayın kuyruğu sonsuza dek kilitliyor ve arkada sahipsiz süreçler bırakıyordu.
- Oynatma listesi eklemek, o istemcinin WebSocket döngüsünü ~2 dk kilitliyordu. Ekleme artık arka planda yapılıyor ve bitince `ack` gönderiliyor.
- **İndirme ve önbellek sorunları:**
  - Aynı adresin eşzamanlı indirmeleri Windows'ta çakışıyordu.
  - Önbellek kontrolü ara ve yarım dosyaları hazır sanıyordu.
  - Farklı paylaşım linkleri (`?si=`) aynı videoyu yeniden indiriyordu.
  
  Dosyalar artık video kimliğiyle adlandırılıyor ve her video için tek bir indirme süreci çalışıyor.
- Yayınlar istemcilere sırayla gönderildiği için tek bir yavaş istemci herkesi (ve botun "şarkı bitti" döngüsünü) bekletiyordu. Artık gönderim eşzamanlı ve istemci başına 2 sn zaman aşımlı.
- Tek bir bozuk mesaj o istemcinin bağlantısını kapatıyordu.
- Durdurduktan sonra gelen ilerleme mesajları boştaki süre göstergesinin üzerine yazıyordu.
- **Bot ses ve katılım düzeltmeleri:**
  - Ses seviyeleri ve mikrofon durumu yeni sayfaya (yeniden katılımda) uygulanmıyordu.
  - Mikrofon düğmesine körlemesine basılıyordu.
  - Yamalanan `getUserMedia` herkese aynı izi veriyordu; Meet'in tek bir `track.stop()` çağrısı botu toplantı boyunca susturuyordu. Artık her çağırana ayrı bir kopya veriliyor.
  - Kamera ancak katıldıktan sonra kapatıldığı için Chrome'un sahte kamera deseni görünebiliyordu. Artık katılma ekranında kapatılıyor.
  - Gürültü giderme anahtarı aranırken yanlış anahtara basılabiliyordu. Arama artık yalnızca ayarlar penceresinde, etikete göre yapılıyor ve sonuç doğrulanıyor.
  - Init script'ler her katılımda üst üste ekleniyor, her iframe kendi AudioContext'ini açıyordu. Artık sekme başına bir kez ekleniyor ve yalnızca üst çerçevede çalışıyor.
- **Bot izleme ve katılım yönetimi:**
  - Katıldıktan sonra toplantı izlenmiyordu: çıkarılma, toplantının bitmesi ya da sekmenin çökmesi durumu "bağlı" bırakıyordu. Artık saniyede bir yoklama yapılıyor ve kopma nedeniyle birlikte bildiriliyor.
  - Toplantıdan ayrılmak ya da link değiştirmek çalan şarkıyı sunucuya haber vermeden kesiyordu; komutlar `about:blank`'e gönderilmeye devam ediyordu.
  - Katılımlar sıraya konmuyordu: eşzamanlı katılımlar aynı sekme için yarışıyor, katılım sırasında gelen bir ayrılma isteği yeniden deneme döngüsü tarafından geri alınıyordu. Artık katılımlar sıralı ve **İptal** edilebiliyor.
  - Botun Chrome penceresi kapatılıp yeniden katılınınca sorun çıkıyordu. Artık bot yeniden bağlanıyor ya da Chrome'u yeniden başlatıyor.
- Çıktı yönlendirildiğinde Türkçe (cp1254) Windows konsolunda emojili `print()` çağrıları açılışı çökertiyordu. Artık konsol UTF-8'e ayarlanıyor ve `logging` kullanılıyor.
- **Arayüz düzeltmeleri:**
  - Özel Tailwind renkleri (teal, fuchsia, sunset) tanımlı olmadığından durum noktası gibi öğeler görünmüyordu.
  - Sürükle-bırak şarkıları çoğaltabiliyordu.
  - Bağlantı koptuğunda 3 sn'de bir hata bildirimi yağıyordu.
  - Ses kaydırıcıları sunucuyu mesaja boğuyordu; odaktaki kaydırıcı uzaktan gelen değişiklikleri yok sayıyordu.
  - *Ekle* düğmesi 3 sn sonra kendiliğinden açılıyor ve linki başarıdan önce siliyordu.
  - "Yükleniyor" durumu yoktu.
  - Değişmeyen Meet linkini tekrar göndermek toplantıya yeniden katılıp şarkıyı kesiyordu.
  - Sürüm etiketleri birbirini tutmuyordu (2.0 / 3.0 / 3.1 / 4.0).
  - Dokunmatik ekranda ilerleme çubuğu ya da ses kaydırıcısı üzerinden başlayan kaydırma şarkıyı sarıyor ya da sesi değiştiriyordu.
  - Hızlı ardışık taşıma komutları hız sınırına takılıp bağlantıyı düşürebiliyordu; sarma çubuğu bazen donuyordu; kuyruktan şarkı kaldırınca klavye odağı kayboluyordu.
  - Bot işi uzun süren komutlar (büyük dosya yükleme, ayrılma) sırasında diğer komutlar yanlışlıkla "Sunucu zamanında yanıt vermedi" hatası veriyordu.
- **Son inceleme turundaki oynatıcı ve bot düzeltmeleri:**
  - Oynatma listesinin ilk şarkısı (ve "şimdi çal" hedefi) sonraki şarkıların önceden indirilmesini bekliyordu; çalınacak şarkı artık öncelikli ve ayrılmış bir indirme yuvası kullanıyor.
  - Kullanıcının duraklattığı müzik, bot yeniden bağlanınca ya da toplantı değişince kendiliğinden başlıyordu.
  - Ctrl+C'ye ikinci kez basmak kapanışı atlatıp botun Chrome'unu toplantıda bırakabiliyordu; oynatıcı ve bot kapanışı artık her durumda çalışıyor. Windows'ta sıfırlanan bir bağlantı yüzünden sunucunun kapanırken sonsuza dek asılı kalması da giderildi (en fazla 10 sn bekleme).
  - Meet'in varsayılan açık *Boş görüşmelerden ayrıl* ayarı, tek kalan botu birkaç dakika sonra çıkarıyordu. Bot artık bu ayarı kapatıyor ve *"Hâlâ orada mısınız?"* sorusunu yanıtlıyor.
  - Toplantı sahibi botu bekleme odasına geri gönderince bot toplantıyı kaybolmuş sayıp sekmeyi bırakıyordu; artık yeniden kabulü bekliyor ve müzik kaldığı yerden sürüyor.
  - Katılma düğmesi yalnızca 6 sabit etiketle aranıyordu; *Switch here*, *Join anyway*, *Join here too / Burada da katıl* gibi varyantlar da artık tanınıyor.
  - `--doctor` artık Node.js sürümünün yt-dlp'nin istediği en az 22 olduğunu da denetliyor.

### Yeni özellikler
- **🖥️ Bot ekranı (yönetici):** Panelden botun tarayıcısı canlı görülür ve fareyle / klavyeyle kullanılır. Ekransız bir sunucuda botun Chrome profilinde Google hesabıyla oturum açmanın yolu budur (Meet, oturum açmamış katılımcıları çoğu toplantıya almaz). Ayrı, temiz bir *Google girişi* sekmesi açılır; oturum durumu gösterilir. Güvenlik: yalnızca yönetici, varsayılan olarak yalnızca sunucunun kendisinden / SSH tüneliyle (`MEETBOT_REMOTE_VIEW`); tıklama ve klavye yalnızca oturum açılmamışken giriş sekmesinde çalışır, oturum açıldığı an giriş sekmesi kapanır ve hesap bağlıyken yeniden açılamaz (hesap ayarlarına panelden erişilemez); Meet sekmesi yalnızca izlenir; **Hesabı çıkar** botun Google/YouTube çerezlerini siler; yazılanlar günlüğe yazılmaz.
- **Linux sunucu desteği:** Ekran yoksa bot Chrome'u kendi açtığı sanal ekranda (**Xvfb**) çalıştırır; Linux'ta `--disable-dev-shm-usage` ve `--password-store=basic` eklenir. `start.sh`, systemd servisi (`deploy/meetbot.service`, `KillMode=mixed` ile temiz kapanış: Chrome ve Xvfb'yi bot kendisi kapatır) ve README'de adım adım sunucu kurulumu. `--doctor` Xvfb'yi de denetliyor. Bot kapanışı, Playwright sürücüsü önce ölse bile Chrome süreç ağacını ve sanal ekranı kapatır.
- **Şarkı adıyla ekleme** (YouTube'daki ilk sonuç) ve **oynatma listeleri** (`MEETBOT_PLAYLIST_LIMIT`, varsayılan 25). `youtu.be`, `shorts/` ve `music.youtube.com` linkleri de destekleniyor; `https://` olmadan da yazılabiliyor.
- **Şu an çalan kartı:** Kapak resmi, ekleyen kişi ve tıklayarak, sürükleyerek ya da klavyeyle **ileri/geri sarılabilen** ilerleme çubuğu. Ayrı bir **YÜKLENİYOR** durumu gösteriliyor.
- **Tekrar modları** (kapalı / şarkı / liste), **karıştır**, **temizle**, **şimdi çal**, dokunmatik ekranda da çalışan yukarı/aşağı düğmeleri, Alt+↑/↓ kısayolu ve kimliğe dayalı sürükle-bırak.
- Kuyrukta toplam kalan süre ve şarkı başına indirme durumu.
- **Son çalınanlar** listesi ve tek tıkla tekrar ekleme; **dinleyenler** listesi.
- **Bot durumu ayrıntıları:** *Tarayıcı hazırlanıyor…*, *Meet açılıyor…*, *Katılma isteği gönderildi, onay bekleniyor…* gibi aşamalar ve kopma nedenleri gösteriliyor. Bağlanırken **İptal** düğmesi var; toplantı değiştirmeden önce onay isteniyor.
- **Misafir / yönetici yetki modeli** ve `MEETBOT_GUEST_CONTROLS`. Misafirler kendi ekledikleri şarkıları kaldırabiliyor.
- **Yapılandırma:** `.env` dosyası ya da ortam değişkenleri (`MEETBOT_*`, bkz. [.env.example](.env.example)). Port, CDP portu, profil ve indirme klasörü, botun adı, zaman aşımları ve sınırlar ayarlanabiliyor.
- **Komut satırı:** `--host`, `--port`, `--doctor` (kurulum denetimi) ve `--fake-bot` (Chrome/Meet olmadan tam arayüz).
- **`start.bat`:** Windows'ta tek tıkla sanal ortam kurulumu ve başlatma; ilk çalıştırmada `.env` oluşturuluyor, `requirements.txt` değiştiğinde bağımlılıklar kendiliğinden güncelleniyor.
- Açılış ekranında yerel ağ adresi ve (üretildiyse) yönetici şifresi gösteriliyor.
- Şarkılar arasındaki ses farkını yumuşatan kompresör (`MEETBOT_NORMALIZE`).
- `GET /api/health` uç noktası.
- **Arayüz bağlantısı ve oturum:**
  - Artan aralıklarla yeniden bağlanan bir bağlantı şeridi ve kalp atışı (ping/pong).
  - Yönetici anahtarı tarayıcıda hatırlanıyor, yeniden bağlanınca otomatik giriş yapılıyor; çıkış yapılabiliyor.
- **Erişilebilirlik:** `aria` etiketleri, canlı bildirim bölgesi, klavyeyle kullanılabilen iletişim kutuları, `prefers-reduced-motion` desteği.
- Windows'ta Ctrl+Break ile de temiz kapanış.

### Değişiklikler
- **Şarkı süresi sınırı kaldırıldı:** `MEETBOT_MAX_DURATION` varsayılanı artık `0` (sınırsız); istenirse saniye cinsinden sınır verilebilir. Uzun videolar için `MEETBOT_DOWNLOAD_TIMEOUT` varsayılanı 300 → 900 sn. Canlı yayınlar yine reddedilir.
- **ffmpeg artık gerekmiyor.** Ses mp3'e dönüştürülmeden, YouTube'un kendi biçiminde (opus/webm ya da m4a) indiriliyor.
- **Node.js (22+) gerekli hale geldi.** YouTube'un JS doğrulaması için yt-dlp'ye `--js-runtimes node` veriliyor (`MEETBOT_YTDLP_JS_RUNTIME`). Çözücü paket `yt-dlp-ejs`, `yt-dlp[default]` ile geliyor.
- yt-dlp artık PATH'te ilk bulunan kopya yerine her zaman sanal ortamdaki paketle çalışıyor (`python -m yt_dlp`). Uyarıları sunucu günlüğüne yazılıyor.
- **Python 3.10+** gerekiyor (önceden 3.9).
- İndirilen dosyalar HTTP üzerinden sunulmuyor; `/downloads` kaldırıldı. Açılışta `downloads/` klasöründeki yalnızca MeetBot dosyaları temizleniyor.
- **WebSocket protokolü v2:** `hello`/`welcome` el sıkışması ve `rid`/`ack` eklendi; mesaj adları değişti (`add_song` → `add`, `loop` → `repeat`, `reorder_queue` → `move` …). 3.1 istemcileriyle uyumlu değil; eşleme tablosu [docs/PROTOCOL.md](docs/PROTOCOL.md)'de.
- **Yeni modül düzeni:**
  - `config.py` (ayarlar), `player.py` (oynatıcı durum makinesi), `fake_bot.py` (sahte bot).
  - Enjekte edilen ses motoru `bot.py` içindeki metinden ayrı bir dosyaya taşındı: `meetbot_inject.js`.
  - Çalışma sırasındaki çıktılar `print` yerine `logging` (`meetbot.*`, `MEETBOT_LOG_LEVEL`) ile yazılıyor.
- **Bot:**
  - CDP portunda zaten bir tarayıcı varsa uyarı verip ona bağlanıyor. O tarayıcıda kendi sekmesini açıyor ve tarayıcıyı hiç kapatmıyor; açık Meet sekmeleri için uyarıyor.
  - Profilinde `meet.google.com` için mikrofon/kamera izni veriyor.
  - `silence.wav` 48 kHz ile otomatik oluşturuluyor (önceden 44,1 kHz'ti ve kod iki yerde tekrarlanıyordu).
- **Arayüz:**
  - Tailwind Play CDN yerine derlenmiş `static/tailwind.css` kullanılıyor.
  - Kullanılmayan Font Awesome kaldırıldı; ikon fontu küçük bir alt kümeye indirildi.
  - Süs görseli gecikmeli yükleniyor.
  - Sürüm her yerde **3.2**.
- **`requirements.txt`:**
  - Alt sürüm sınırları eklendi.
  - Kullanılmayan `aiofiles` ve `uvicorn[standard]` ile zaten gelen `websockets` çıkarıldı.
  - `yt-dlp` yerine `yt-dlp[default]` kullanılıyor.
- MIT `LICENSE` dosyası eklendi. Kök dizindeki boş `python` dosyası silindi. `.gitattributes`'a `*.bat` için CRLF kuralı eklendi.
- README baştan yazıldı: Kurulum, yapılandırma, yetki modeli, mimari ve sorun giderme bölümleri eklendi; eski `password123` ve ffmpeg bilgileri çıkarıldı.

### Geliştirici
- **Test paketi (pytest + pytest-asyncio), 500'ü aşkın test:**
  - `FakeBot` ve sahte indiriciyle oynatıcı değişmezleri (yarışlar, jetonlar, tekrar modları, sınırlar, dosya temizliği).
  - `TestClient` ile WebSocket protokolü testleri (yetkiler, sıralama, hız sınırı, Origin).
  - `audio_manager` testleri: sorgu sınıflandırma, argüman sertleştirme, süreç ağacının öldürülmesi.
  - Headless Chromium'da sahte bir Meet sayfasına (`tests/mock_meet/meet.html`) karşı bot akışı ve enjekte edilen ses motorunun testleri.
  - Sahte bir protokol-v2 sunucusuna karşı arayüz testleri.
  - Uçtan uca testler: gerçek sunucu + gerçek arayüz, aynı anda iki tarayıcı (yönetici + misafir), XSS denemesi dahil.
  - Gerçek `MeetBot` + `Player` ile sahte Meet sayfasında tam ses zinciri testi (analizörle sesin gerçekten aktığı doğrulanıyor).
- `pytest.ini` işaretleri: `browser` (Playwright Chromium; yoksa otomatik atlanır) ve `network` (gerçek YouTube; `-m "not network"` ile hariç tutulur).
- `requirements-dev.txt` (`pytest`, `pytest-asyncio`, `httpx`).
- **Kod yapısı:** Uygulama fabrikası `create_app(settings, player, bot, hub)` eklendi. Modüllerin içe aktarılırken yan etkisi yok; bot ve indirici dışarıdan veriliyor.
- **Tailwind derlemesi:** `package.json` (`npm run build:css` / `watch:css`, `npx` ile sabitlenmiş `tailwindcss@3.4.19`), `tailwind.config.js` ve `static/src/input.css`.
- Protokol başvuru belgesi: [docs/PROTOCOL.md](docs/PROTOCOL.md).

## [3.1] - 2026-02-22

Önceki sürüm. Neon Vaporwave arayüzü, YouTube linkleriyle müzik kuyruğu (yt-dlp ile mp3'e dönüştürme), Web Audio ile Meet'e ses enjeksiyonu, müzik/mikrofon ses kontrolü, döngü modu ve istemci tarafında şifre soran yönetici paneli.
