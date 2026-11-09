# Esnaf Randevu Asistanı — WhatsApp Hatırlatıcı + Google Yorum Toplama (MVP)

Yerel esnaflar (kuaför, klinik, güzellik salonu, kuaför vb.) için **tek komutla çalışan**,
tek kullanıcılı ve hafif bir panel. Müşteri randevusu eklenir; uygulama arka planda
SQLite'ı kontrol ederek:

1. randevudan **2 saat önce** WhatsApp'tan onay mesajı,
2. randevu bittikten **1 saat sonra** Google yorum teklifi mesajı

gönderir. Panelde tüm randevular, mesaj durumları ve gönderim günlüğü görünür.

```
Merhaba {isim}, {ne_zaman} saat {saat} randevunuzu onaylıyor musunuz?
```
```
Merhaba {isim}, hizmetimizi değerlendirmek için 1 dakikanızı ayırır mısınız? Google Yorum Linki: {link}
```

## Özellikler

- **İki sayfa:** Panel (`/`) ve Ayarlar (`/ayarlar`). Üyelik, JWT, e-posta doğrulama yok.
- **SQLite:** tek dosya (`data/esnaf.db`), ek kurulum yok.
- **Arka plan motoru:** 30 saniyede bir kontrol; aynı hatırlatma atomik olarak
  sahiplenilir, böylece panel motoru ile ayrı scheduler/cron aynı anda çalışsa bile
  **çift gönderim olmaz**. Hatalı gönderimler kademeli olarak yeniden denenir.
- **Ayrı scheduler (`scheduler.py`):** Panel kapalıyken bile çalışan, tekil örnek
  kilitli arka plan döngüsü. `python scheduler.py` veya cron ile `--once`.
- **Güvenlik:** Formlarda CSRF anahtarı, `Host` başlığı doğrulaması (DNS rebinding
  koruması), isteğe bağlı parola (`ESNAF_PASSWORD`, LAN'da zorunlu), CSP başlıkları,
  yalnızca yerel ağa bağlanma; Flask hata ayıklayıcısı kapalı.
- **Üç WhatsApp sağlayıcısı:** simülasyon (varsayılan), pywhatkit, HTTP ağ geçidi (Baileys/WAHA).
- **Manuel kontrol:** panelden "Onay mesajını şimdi gönder", "Motoru şimdi çalıştır",
  test mesajı.
- **Türkçe arayüz**, mobil uyumlu, Tailwind CSS (yerelde derlenmiş `static/app.css`, internet gerekmez).
- **`python main.py`** ile çalışır; Docker, bulut veya harici servis zorunluluğu yok.

## Hızlı başlangıç

Gereken: Python 3.10+ (3.12/3.14 test edildi).

```bash
# 1) Proje klasöründe sanal ortam (önerilir)
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 2) Bağımlılıklar
pip install -r requirements.txt

# 3) Panel + otomatik motor
python main.py
```

Tarayıcıda **http://127.0.0.1:5000** açılır (açılmazsa adresi elle girin).

İlk deneme için örnek verilerle başlatın:

```bash
python main.py --demo
```

Panel dolu gelir; simülasyon modunda motor bir sonraki turda (≤30 sn) örnek
onay/yorum mesajlarını "gönderir" ve günlüğe yazar.

### Komut satırı seçenekleri

| Komut | Açıklama |
|---|---|
| `python main.py` | Paneli + arka plan motorunu başlatır |
| `python main.py --demo` | Örnek müşteri/randevular ekler |
| `python main.py --port 8080` | Farklı port |
| `python main.py --no-browser` | Tarayıcıyı otomatik açmaz |
| `python main.py --no-worker` | Yalnızca panel (otomatik gönderim yok) |
| `python main.py --tick-once` | Motoru bir kez çalıştırıp çıkar (cron / Görev Zamanlayıcı) |
| `python main.py --debug` | Ayrıntılı günlük (Flask hata ayıklayıcısı kapalı kalır) |

> **Güvenlik:** Panel varsayılan olarak yalnızca `127.0.0.1` adresini dinler. İnternete
> açmayın. `--host 0.0.0.0` ile yerel ağa açmak için `.env` dosyasında
> `ESNAF_PASSWORD` tanımlı olmalıdır (yoksa panel başlamaz) ve
> `ESNAF_TRUSTED_HOSTS` ortam değişkenine bilgisayarın IP'si eklenmelidir.
> Ayrıntılar: [Güvenlik notları](#güvenlik-notları).

## WhatsApp gönderim modları

Ayarlar → **WhatsApp bağlantısı** bölümünden seçilir. Varsayılan **simülasyon**dur;
kurulumun ve panelin tamamı WhatsApp hesabı olmadan test edilebilir.

### 1) Simülasyon (varsayılan)

Gerçek mesaj gönderilmez; mesaj panel günlüğüne "Simüle" olarak yazılır.
Mesaj şablonlarını, zamanlamayı ve panel akışını denemek için idealdir.

### 2) pywhatkit (WhatsApp Web, masaüstü)

- Gereksinim: bilgisayarda **WhatsApp Web'e giriş yapılmış** bir tarayıcı, masaüstü oturumu.
- `pip install -r requirements-whatsapp.txt` pywhatkit'i kurar (çekirdek kurulumda
  isteğe bağlıdır; kurmazsanız panelin diğer her şeyi çalışır).
- Gönderim sırasında tarayıcı açılır, mesaj yazılır ve sekme kapanır. Bilgisayar uykuya
  girerse veya tarayıcı kapanırsa gönderim başarısız olur; motor yeniden dener.
- Bu mod yerel masaüstü için pratiktir; 7/24 sunucu için HTTP ağ geçidi daha uygundur.

### 3) HTTP ağ geçidi (Baileys / Puppeteer / WAHA)

Ayrı çalışan bir WhatsApp ağ geçidine JSON POST eder. Ayarlar'da **HTTP** seçilir,
adres/token girilir. İki istek biçimi vardır:

**generic** — adres tam uç noktadır (ör. `http://127.0.0.1:3000/send`):

```http
POST /send
Content-Type: application/json
Authorization: Bearer <token>        # opsiyonel

{"to": "905551112233", "phone": "905551112233", "message": "Merhaba ..."}
```

**WAHA** — adres sunucu köküdür (ör. `http://127.0.0.1:3000`); uygulama
`/api/sendText` adresine şu gövdeyi gönderir:

```json
{"session": "default", "chatId": "905551112233@c.us", "text": "Merhaba ..."}
```

Her iki modda da **2xx** yanıt başarı kabul edilir; hata gövdesi panelde gösterilir.
Ağ geçidi kapalıysa motor 5 dk arayla (ayarlanabilir) yeniden dener.

## Zamanlama mantığı

| İş | Ne zaman | Varsayılan |
|---|---|---|
| `confirmation` | `randevu başlangıcı − X saat` | 2 saat önce |
| `review` | `randevu bitişi + Y saat` | 1 saat sonra |

- Randevu süresi ayrı tutulur (varsayılan 60 dk); bitiş = başlangıç + süre.
- Randevu eklendiğinde/güncellendiğinde iki iş planlanır. Saat değişirse bekleyen işler
  yeniden hesaplanır; **gönderilmiş mesaj tekrarlanmaz**. Gönderim anında hatırlatma
  atomik olarak sahiplenildiği için aynı mesajı iki süreç (panel + cron) aynı anda
  göndermeye çalışsa bile yalnızca biri gönderir.
- Randevu başladıktan sonra onay mesajı gönderilmez (atlanır).
- İptal / "gelmedi" durumunda bekleyen mesajlar atlanır. İptal edilen randevu
  "Yeniden aç" ile geri alınırsa gelecekteki mesajlar yeniden planlanır.
- Google yorum linki boşsa gerçek gönderim modunda yorum mesajı atlanır; Ayarlar'dan link
  eklendiğinde bu nedenle atlanmış yorum mesajları otomatik olarak yeniden planlanır.
- Başarısız gönderim: `maks. deneme` (varsayılan 3) × `tekrar aralığı` (varsayılan 5 dk).
- Panelden "şimdi gönder" ile zamanı beklemeden elle tetiklenebilir.

## Streamlit paneli (telefondan hızlı kullanım)

Esnafın veya çırağın telefondan saniyeler içinde randevu girebileceği sade bir ekran.
Flask paneliyle aynı veritabanını kullanır; ikisi aynı anda açık olabilir.

```bash
pip install -r requirements-streamlit.txt
streamlit run streamlit_app.py
```

Ekranda:

- **Sistem durumu:** 🟢 Sistem Çevrimiçi / 🟡 Simülasyon / 🔴 Sistem Çevrimdışı.
  WhatsApp ağ geçidi yoklanır (WAHA'da `GET /api/sessions/{oturum}` → `WORKING`,
  genel Baileys ağ geçidinde adresin yanıt verip vermediği) ve hatırlatma motorunun
  son kontrol zamanına (heartbeat) bakılır.
- **Randevu kartları (Bugün / Yarın):** Telefonda kenarlara taşmayan, tablo yerine
  kart görünümü. Her kartta saat ve müşteri, renkli durum etiketleri (✅ Onayladı,
  ⏳ Cevap yok, 📩 Hatırlatma gitti, ❌ İptal), dokununca arayan telefon numarası ve
  48 px yüksekliğinde iki düğme bulunur. Liste 15 saniyede bir yenilenir.
- **📲 Hatırlat düğmesi:** "Randevunuzu unutmayın" mesajını tek dokunuşla gönderir.
  Otomatik onay/yorum işlerini etkilemez. Yanlışlıkla iki kez dokunmaya karşı, 2 dakika
  içinde aynı müşteriye ikinci gönderim için onay ister. Metin, Flask panelinde
  Ayarlar → Mesaj şablonları → "Manuel hatırlatma mesajı" alanından değiştirilir.
- **✅ Onayladı düğmesi:** Müşteri telefonla/WhatsApp'tan onay verdiğinde randevuyu
  onaylandı olarak işaretler (gelen WhatsApp cevapları otomatik okunmaz).
- **Hızlı randevu:** ad, telefon, gün ve saat. Kurallar Flask paneliyle aynıdır
  (`booking.py`).
- Hiç zamanlayıcı çalışmıyorsa Streamlit kendi içinde bir tane başlatır; `main.py` veya
  `scheduler.py` zaten çalışıyorsa tek örnek kilidi sayesinde ikinci motor açılmaz.
  Kapatmak için `ESNAF_STREAMLIT_SCHEDULER=0`.

**Telefondan erişim:** Varsayılan olarak yalnızca bu bilgisayardan açılır
(`.streamlit/config.toml`). Telefondan kullanmak için `.env` içinde `ESNAF_PASSWORD`
tanımlayın ve şöyle başlatın:

```bash
streamlit run streamlit_app.py --server.address 0.0.0.0
```

Telefonda `http://<bilgisayarın-IP-adresi>:8501` adresini açın. Parola yoksa panel
yerel ağda açılmayı reddeder. Bağlantı şifresiz HTTP'dir; yalnızca güvendiğiniz
ağda (dükkânın Wi-Fi'ı) kullanın.

## Arka plan zamanlayıcısı (`scheduler.py`)

WhatsApp kontrol döngüsü ayrı bir dosyadadır. Panel (`main.py`) açıldığında scheduler
otomatik olarak arka planda başlar; isterseniz panelden bağımsız da çalıştırabilirsiniz:

```bash
python scheduler.py                  # sürekli döngü (varsayılan 30 sn)
python scheduler.py --interval 15    # 15 saniyede bir kontrol
python scheduler.py --once           # bir tur çalıştırıp çıkar (cron / Görev Zamanlayıcı)
python scheduler.py --force          # "otomatik gönderim kapalı" ayarını yok say
```

- **Tek örnek kilidi:** Aynı veritabanı için ikinci bir scheduler başlatılamaz
  (`data/scheduler.lock`). Panel açıkken ayrıca scheduler çalıştırırsanız panel
  "Harici scheduler çalışıyor" gösterir ve kendi kopyasını başlatmaz.
- **Heartbeat:** Son tur zamanı ve özeti `scheduler.last_tick_at` /
  `scheduler.last_tick_summary` meta anahtarlarında tutulur; panel üst şeridinde
  "Son kontrol" olarak görünür.
- **Zarif kapanış:** Ctrl+C / SIGTERM ile döngü durur, kilit serbest bırakılır.
- Gönderim iş mantığı (`worker.py`) ile döngü (`scheduler.py`) ayrıdır; test etmek
  veya farklı bir zamanlayıcıya taşımak kolaydır.

## Ayarlar

Panel → **Ayarlar**:

- İşletme adı, saat dilimi (varsayılan `Europe/Istanbul`), Google yorum linki
- Onay mesajı öne alması / yorum mesajı gecikmesi / varsayılan süre
- WhatsApp sağlayıcısı + HTTP ağ geçidi bilgileri
- Mesaj şablonları: `{isim}`, `{ne_zaman}` (bugün / yarın / tarih), `{saat}`, `{tarih}`, `{gun}`, `{isletme}`, `{link}`,
  `{hizmet}`, `{telefon}` (büyük/küçük harf ve Türkçe karakter duyarsız)
- Otomatik gönderim aç/kapat, deneme sayısı, tekrar aralığı
- Test mesajı, motor durumu, "Motoru şimdi çalıştır"

Tüm ayarlar `settings` tablosunda saklanır; `.env` yalnızca altyapı içindir
(`.env.example` dosyasına bakın). HTTP API anahtarı arayüzde gösterilmez; boş
bırakılan alan kayıtlı anahtarı korur.

## Güvenlik notları

- **Parola:** `ESNAF_PASSWORD` tanımlıysa tüm sayfalar tek parolayla korunur
  (5 hatalı denemede 5 dakika kilit, parola değişince eski oturumlar düşer).
  Sunucu varsayılan olarak `127.0.0.1` dinler; `--host 0.0.0.0` ile LAN'a açmak için
  parola zorunludur. Bağlantı şifresiz HTTP'dir; yalnızca güvendiğiniz ağda kullanın.
- **API anahtarı:** HTTP API adresi değiştirildiğinde kayıtlı anahtar silinir; eski
  anahtar yeni sunucuya gönderilmez. Anahtar şablonlara hiç aktarılmaz.
- **Üçüncü taraf script yok:** CSS yerelde derlenir; `Content-Security-Policy` yalnızca
  panelin kendi dosyalarına izin verir, `X-Frame-Options: DENY` ile iframe engellenir.
- **CSRF koruması:** Tüm POST formları oturuma bağlı tek kullanımlık anahtar taşır;
  anahtarsız istekler 400 ile reddedilir. Oturum çerezi `HttpOnly` + `SameSite=Lax`.
- **Host doğrulaması:** `TRUSTED_HOSTS` varsayılanı `127.0.0.1, localhost`tur; yabancı
  `Host` başlıkları (DNS rebinding) reddedilir. LAN kullanımı için
  `ESNAF_TRUSTED_HOSTS` ortam değişkenine IP ekleyin.
- **Hata ayıklayıcı kapalı:** `--debug` yalnızca günlük seviyesini artırır; Flask/Werkzeug
  etkileşimli hata ayıklayıcısı hiçbir modda açılmaz.
- **Veri:** Müşteri adı, telefonu ve mesaj gövdesi `data/` altındaki SQLite dosyasında
  düz metindir. Bilgisayara erişimi olan herkes okuyabilir; `data/`yı ve yedekleri aynı
  özenle saklayın. Günlük dosyasına (`app.log`) telefon numarası yalnızca maskeli
  (`+90 555 *** ** 33`) yazılır, mesaj metni hiç yazılmaz.
- **Mesaj izni (KVKK / İYS):** Bir müşteri mesaj almak istemezse panelde randevunun
  "İşlemler" menüsünden (telefonda kartın "⋯ Diğer" bölümünden) **Müşteri mesaj
  istemiyor** seçin. O müşteriye otomatik veya manuel hiçbir mesaj gönderilmez.
- **Saklama süresi:** Mesaj günlüğü varsayılan olarak 365 gün tutulur, sonra her gün
  otomatik silinir (`message_retention_days` ayarı).
- Panel internete açılmamalıdır; ihtiyaç varsa önüne kimlik doğrulamalı bir ters vekil
  (nginx + basic auth vb.) konulmalıdır.

## Proje yapısı

```
main.py          # giriş noktası: DB, scheduler thread'i, Flask sunucusu
scheduler.py     # arka plan döngüsü, tekil örnek kilidi, heartbeat, CLI
app.py           # Flask rotaları + şablon filtreleri (2 sayfa + giriş)
streamlit_app.py # telefondan hızlı randevu / durum paneli (isteğe bağlı)
booking.py       # randevu oluşturma kuralları (iki panel ortak)
auth.py          # panel parolası ve deneme sınırı (iki panel ortak)
db.py            # SQLite şeması ve tüm veri işlemleri
whatsapp.py      # telefon normalizasyonu, şablon, 3 gönderim sağlayıcısı
worker.py        # hatırlatma iş mantığı (gönderim, retry, sahiplenme)
utils.py         # UTC/yerel saat, Türkçe tarih biçimlendirme
config.py        # yollar, .env, loglama, sunucu ayarları
templates/       # base.html, index.html, settings.html, error.html
static/app.js    # bildirim kapatma, silme onayı, menü davranışları
static/app.css   # derlenmiş Tailwind stilleri (kaynak: tailwind/)
tests/           # unittest paketi
data/            # çalışma zamanında oluşur: esnaf.db, logs/, scheduler.lock
```

### Veritabanı şeması (özet)

- `customers(id, name, phone UNIQUE, notes, created_at, updated_at)`
- `appointments(id, customer_id, starts_at, duration_minutes, service, notes, status, ...)`
- `reminders(id, appointment_id, kind, due_at, status, attempts, next_attempt_at, sent_at, ...)`
  — `UNIQUE(appointment_id, kind)` ve atomik sahiplenme (`claim_reminder`) ile
  çift gönderim engellenir
- `messages(id, appointment_id, customer_id, kind, phone, body, provider, status, simulated, error, created_at)`
- `settings(key, value)` · `meta(key, value, updated_at)`

Tüm zamanlar **UTC** saklanır, arayüzde işletmenin saat dilimine çevrilir.

## Yedekleme

Tüm veri tek dosyada: `data/esnaf.db`.

- **Otomatik yedek:** Motor her gün ilk turunda çalışırken bile tutarlı bir kopya alır:
  `data/backups/esnaf-YYYYMMDD-HHMMSS.db`. Son 14 yedek tutulur (`backup_keep_days`).
  Farklı bir klasör (ör. USB disk / OneDrive) için `ESNAF_BACKUP_DIR` tanımlayın.
- **Geri yükleme:** Paneli kapatın, istediğiniz yedeği `data/esnaf.db` olarak kopyalayın.

## Testler

```bash
python -m unittest discover -s tests -t .
```

Kapsam: telefon/şablon/sağlayıcı birim testleri, SQLite hatırlatma planı, atomik
sahiplenme, motorun tekrar göndermeme davranışı, hata/yeniden deneme, scheduler
döngüsü ve tekil örnek kilidi, Flask form doğrulama, CSRF, güvenilmeyen Host,
açık yönlendirme koruması, ayarlar, manuel gönderim ve demo verisi.

## 7/24 çalıştırma (opsiyonel)

Panel açıkken döngü zaten panelin içinde çalışır. Panel kapalıyken de mesaj
gönderilmesi gerekiyorsa iki seçenek vardır:

**A) Scheduler'ı ayrı süreçte çalıştırın (önerilir):**

```bash
python scheduler.py
```

Windows'ta başlangıçta otomatik başlatmak için `pythonw.exe scheduler.py`
kısayolu veya Görev Zamanlayıcı'da "başlangıçta çalıştır" görevi kullanılabilir.

**B) Periyodik görevle tek tur çalıştırın:**

```bat
cd /d C:\esnafspor
.venv\Scripts\python.exe scheduler.py --once
```

Linux/macOS cron:

```cron
*/5 * * * * cd /opt/esnafspor && .venv/bin/python scheduler.py --once >> data/logs/cron.log 2>&1
```

> Scheduler tekil örnek kilidi kullandığı için panel + ayrı scheduler aynı anda
> çalıştırılsa bile ikinci döngü başlamaz; mesajlar atomik sahiplenme ile yine
> yalnızca bir kez gönderilir. `--once` cron görevini panel açıkken çalıştırmak da
> güvenlidir (çift gönderim olmaz), yine de tek yöntem seçmeniz önerilir.

## Sık sorulanlar

**Telefon numarası hangi formatta olmalı?**
`0555 111 22 33`, `+90 555 111 22 33`, `90 555 111 22 33` veya `5551112233`
kabul edilir; `+90` formatına normalize edilir. Yalnızca Türk numaraları desteklenir.

**Panelde "Motor durduruldu" yazıyor.**
`--no-worker` ile başlatılmış olabilir. Normal başlatmada motor thread'i otomatik çalışır;
Ayarlar'dan otomatik gönderim kapatıldıysa üst şeritte "Otomatik gönderim kapalı" görünür.

**Mesajlar gerçekten gitmiyor.**
Varsayılan sağlayıcı simülasyondur. Ayarlar → WhatsApp bağlantısı'ndan pywhatkit veya
HTTP seçin ve test mesajı gönderin.

**Stilleri değiştirmek istiyorum.**
Arayüz stilleri `static/app.css` dosyasındadır ve internet gerektirmez. Şablonlarda yeni
Tailwind sınıfları kullandıysanız CSS'i yeniden derleyin (Node.js gerekir):
`cd tailwind && npm install && npm run build`.

**WhatsApp kullanım koşulları?**
Bu araç, kendi işletmenizin müşterilerine hatırlatma göndermek için tasarlanmıştır.
Spam/izin dışı mesaj göndermek WhatsApp kullanım koşullarına aykırıdır; sorumluluk
kullanıcıya aittir.
