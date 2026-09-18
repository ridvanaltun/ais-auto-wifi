# AIS Wi-Fi Auto-Login 🛜

macOS menü çubuğunda (tray) çalışan, **AIS SUPER WiFi** gibi captive portal'lı
ücretsiz Wi-Fi ağlarında bağlantı koptuğunda **otomatik olarak yeniden giriş
yapan** küçük bir uygulama.

Kafede oturuyorsun, 30 dakikada bir bağlantı kopuyor ve her seferinde telefon
numarası + SMS OTP girmek zorunda kalıyorsun. Bu uygulama arka planda bağlantıyı
sürekli izler; kopmayı fark ettiği an portalı bulur, kimlik bilgilerinle giriş
yapar ve tekrar çevrimiçi olduğunu doğrular. Menü çubuğundaki simge o an ne
durumda olduğunu gösterir.

> **Not:** Bu uygulama, senin zaten yasal olarak kullandığın bir Wi-Fi ağına,
> senin kendi numaran ve kimlik bilgilerinle giriş yapmayı otomatikleştirir.
> Herhangi bir güvenlik önlemini aşmaz, şifre kırmaz, başkasının hesabını
> kullanmaz.

---

## Simgeler ne anlama geliyor?

| Simge | Durum |
|-------|-------|
| 🟢 | Çevrimiçi — her şey yolunda |
| 🔴 | Bağlantı yok |
| 🔓 | Captive portal algılandı (giriş gerekiyor) |
| ⏳ | Giriş yapılıyor… |
| ⚠️ | Hata (menüden "Durum" ile ayrıntıya bak) |

---

## Kurulum

### 1. Gereksinimler

- macOS
- Python 3.9 veya üzeri (`python3 --version` ile kontrol et)

### 2. Bağımlılıkları kur

Bu klasörün içinde bir Terminal aç ve:

```bash
pip3 install -r requirements.txt
```

### 3. Önce teşhis çalıştır (arayüz açmadan test)

```bash
python3 run.py --diagnose
```

Bu komut; SSID'yi, Wi-Fi arayüzünü, bağlantı durumunu, algılanan sağlayıcıyı,
kayıtlı kimlik bilgilerinin olup olmadığını ve son 5 dakikadaki SMS OTP'yi
gösterir. Sorun varsa nerede olduğunu buradan anlarsın.

### 4. Uygulamayı başlat

```bash
python3 run.py
```

Menü çubuğunda 🛜 simgesi belirir. İlk açılışta sağ üstteki simgeye tıkla.

---

## İlk kullanım

1. **Kimlik Bilgilerini Gir…** menüsünden telefon numaranı (ve şifre yöntemini
   seçtiysen şifreni) gir. Bunlar **macOS Keychain**'e kaydedilir, düz metin
   olarak hiçbir dosyada tutulmaz.
2. **Giriş Yöntemi** alt menüsünden birini seç:
   - **Şifre (önerilen):** AIS portalında numara + şifre ile tek adımda,
     tamamen otomatik giriş. SMS beklemek gerekmez. *En sorunsuz yöntem budur.*
     (AIS uygulamasından/portalından bir Wi-Fi şifresi belirleyebilirsin.)
   - **SMS OTP:** Numaranı girer, portal SMS gönderir, uygulama kodu Mesajlar'dan
     okuyup otomatik girer (aşağıdaki izinler gerekir). Okuyamazsa sana sorar.
3. **Otomatik Bağlan** açık olduğu sürece uygulama kopmaları kendi halleder.
   İstediğin an **Şimdi Bağlan** ile elle de tetikleyebilirsin.

---

## SMS OTP'nin otomatik okunması (opsiyonel ama önerilir)

Sadece **SMS OTP** yöntemini kullanacaksan gerekli. Uygulama, gelen kodu
Mac'teki **Mesajlar** uygulamasının veritabanından okur. Bunun için:

### A) iPhone → Mac SMS yönlendirme
iPhone'da: **Ayarlar → Mesajlar → Metin Mesajı Yönlendirme** → Mac'ini aç.
Böylece AIS'ten gelen SMS kodu Mac'teki Mesajlar'a da düşer.

### B) Tam Disk Erişimi (Full Disk Access)
Mesajlar veritabanını okuyabilmek için Terminal'e (ya da uygulamayı
paketlediysen o .app'e) izin ver:
**Sistem Ayarları → Gizlilik ve Güvenlik → Tam Disk Erişimi** → Terminal'i ekle
ve işaretle. Sonra Terminal'i kapatıp yeniden aç.

Bu izinleri vermek istemezsen sorun değil: OTP yöntemi seçili olduğunda kodu
okuyamazsa uygulama sana küçük bir pencerede kodu **elle** sorar. Ya da hiç
uğraşmamak için **Şifre** yöntemini kullan.

---

## SSID okuma ve Konum izni

macOS, bağlı olduğun Wi-Fi ağının adını (SSID) okuyabilmek için **Konum
Servisleri** iznini ister. İzin verilmezse uygulama yine çalışır; sadece "hangi
ağdayım" bilgisini portal adresinden/HTML'den tespit eder. SSID'yi net görmek
için: **Sistem Ayarları → Gizlilik ve Güvenlik → Konum Servisleri**.

---

## Açılışta otomatik başlatma

Bilgisayarı her açtığında uygulamanın kendiliğinden başlaması için birlikte gelen
`com.aiswifi.autologin.plist` dosyasını kullan:

1. Dosyayı bir metin düzenleyicide aç, içindeki iki YOLU kendi bilgisayarına göre
   düzelt (Python yolu için `which python3`, klasör yolu için bu klasörde `pwd`).
2. Kopyala ve yükle:
   ```bash
   cp com.aiswifi.autologin.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.aiswifi.autologin.plist
   ```
3. Kaldırmak istersen:
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.aiswifi.autologin.plist
   ```

---

## İleride başka Wi-Fi noktaları eklemek

Mimari bunun için tasarlandı. Her ağ bir "sağlayıcı" (provider) olarak
`aiswifi/providers/` altında yaşar. Yeni bir ağ eklemek için:

1. `aiswifi/providers/ais.py` dosyasını örnek al, yeni bir dosya oluştur
   (örn. `truewifi.py`), `BaseProvider`'dan türet.
2. `matches()` içinde o ağı nasıl tanıyacağını yaz (SSID adı, portal alan adı
   veya sayfa HTML'i).
3. `aiswifi/providers/__init__.py` içindeki `build_registry()` listesine ekle
   (her zaman `GenericProvider` en sonda kalsın — o, tanınmayan portallar için
   son çare olarak genel HTML-form giriş motorunu kullanır).

Çoğu basit captive portal için hiçbir şey yazmana bile gerek kalmayabilir:
`GenericProvider` formu otomatik bulup numara/şifre/OTP alanlarını tahmin ederek
denemesini yapar.

---

## Dosya ve kayıt yerleri

- Ayarlar: `~/.config/aiswifi/config.json`
- Günlük (log): `~/.config/aiswifi/aiswifi.log` (menüden **Kayıtları Aç**)
- Kimlik bilgileri: **macOS Keychain** (`aiswifi` servisi) — dosyada değil.

---

## Sık karşılaşılan sorunlar

- **"rumps gerekli" hatası:** `pip3 install rumps` (ya da `pip3 install -r
  requirements.txt`).
- **SSID hep boş görünüyor:** Konum izni kapalı olabilir; sorun değil, giriş yine
  çalışır.
- **OTP otomatik okunmuyor:** Metin Mesajı Yönlendirme ve Tam Disk Erişimi
  açık mı? Değilse uygulama kodu elle soracaktır. Ya da Şifre yöntemine geç.
- **Giriş başarısız / hata simgesi:** `python3 run.py --diagnose` çıktısına ve
  **Kayıtları Aç** ile log dosyasına bak.

---

## Komutlar özeti

```bash
python3 run.py             # menü çubuğu uygulamasını başlat
python3 run.py --diagnose  # arayüz açmadan ağ/OTP/SSID teşhisi
python3 run.py --version   # sürüm
```
