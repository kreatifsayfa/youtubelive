# YouTube Live Streamer

YouTube'a canlı yayın göndermek için basit ve kullanışlı bir web uygulaması. M3U8 URL'leri, video dosyaları kullanarak YouTube'a stream yapabilirsiniz.

![YouTube Live Streamer](https://img.shields.io/badge/Python-3.8+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

## Özellikler

- ✅ **M3U8 Stream Desteği**: İnternetten M3U8 URL'i ile yayın
- ✅ **Video Dosyası Yükleme**: MP4, MKV, AVI ve daha fazla format
- ✅ **Döngü (Loop) Modu**: Videoyu sürekli tekrar et
- ✅ **Kalite Seçimi**: 720p, 1080p, 1440p, 4K seçenekleri
- ✅ **Modern Web Arayüzü**: Kullanımı kolay, responsive tasarım
- ✅ **Gerçek Zamanlı Durum**: Yayın durumunu anlık izleme
- ✅ **FFmpeg Entegrasyonu**: Güçlü video işleme

## Gereksinimler

### Sistem Gereksinimleri

- **Python 3.8+**
- **FFmpeg** (video işleme için)
- **Linux İşletim Sistemi** (Ubuntu, Debian, Fedora, Arch, vb.)

### Python Kütüphaneleri

```bash
pip install -r requirements.txt
```

## Kurulum

### 1. FFmpeg Kurulumu

**Ubuntu/Debian:**
```bash
sudo apt update
sudo apt install ffmpeg
```

**Fedora:**
```bash
sudo dnf install ffmpeg
```

**Arch Linux:**
```bash
sudo pacman -S ffmpeg
```

FFmpeg kurulumunu kontrol edin:
```bash
ffmpeg -version
```

### 2. Python Kurulumu

Eğer Python yüklü değilse:

**Ubuntu/Debian:**
```bash
sudo apt install python3 python3-pip python3-venv
```

**Fedora:**
```bash
sudo dnf install python3 python3-pip
```

**Arch Linux:**
```bash
sudo pacman -S python python-pip
```

### 3. Projeyi İndirin

```bash
git clone <repo-url>
cd youtubelive
```

veya ZIP olarak indirip açın.

### 4. Sanal Ortam Oluşturun (Önerilen)

```bash
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
```

### 5. Bağımlılıkları Yükleyin

```bash
pip install -r requirements.txt
```

## Kullanım

### 1. Uygulamayı Başlatın

```bash
python3 app.py
```

Alternatif olarak, arka planda çalıştırmak için:
```bash
nohup python3 app.py > app.log 2>&1 &
```

### 2. Web Arayüzünü Açın

Tarayıcınızda aşağıdaki adresi açın:
```
http://localhost:5000
```

Aynı ağdaki başka bir cihazdan erişmek için:
```
http://<sunucu-ip-adresi>:5000
```

### 3. YouTube Stream Key'inizi Alın

1. [YouTube Studio](https://studio.youtube.com) açın
2. Sol menüden **"Oluştur"** > **"Canlı yayına geç"** tıklayın
3. Yeni bir yayın oluşturun veya mevcut birini seçin
4. **"Stream ayarları"** bölümünden **"Stream anahtarı"** (Stream key) kopyalayın

⚠️ **Önemli**: Stream anahtarınızı kimseyle paylaşmayın!

### 4. Yayın Başlatın

#### M3U8 URL ile:
1. "YouTube Stream Key" alanına anahtarınızı girin
2. "M3U8 URL" sekmesine tıklayın
3. M3U8 stream URL'inizi girin
4. Kaliteyi seçin
5. **"Yayını Başlat"** butonuna tıklayın

#### Video Dosyası ile:
1. "YouTube Stream Key" alanına anahtarınızı girin
2. "Dosya Yükle" sekmesine tıklayın
3. Video dosyanızı sürükleyip bırakın veya seçin
4. İsteğe bağlı: "Döngü" seçeneğini işaretleyin
5. Yüklenen dosyayı seçin
6. **"Yayını Başlat"** butonuna tıklayın

### 5. Yayını İzleyin

YouTube Studio'dan veya kanalınızdan canlı yayınınızı izleyebilirsiniz.

## Desteklenen Video Formatları

- MP4
- MKV
- MOV
- AVI
- FLV
- WMV
- WebM
- M3U8

## Kalite Ayarları

| Kalite | Çözünürlük | Bitrate |
|--------|------------|---------|
| 720p   | HD         | 2.5 Mbps |
| 1080p  | Full HD    | 4.5 Mbps |
| 1440p  | 2K         | 9 Mbps   |
| 2160p  | 4K         | 18 Mbps  |

## Sorun Giderme

### FFmpeg bulunamadı hatası

Eğer "FFmpeg not found" hatası alıyorsanız:

1. FFmpeg'in kurulu olduğundan emin olun:
```bash
ffmpeg -version
```

2. FFmpeg PATH'e ekli değilse:
```bash
which ffmpeg
```
Komutu ile konumunu bulun ve PATH'e ekleyin.

### Port zaten kullanımda

5000 portu başka bir uygulama tarafından kullanılıyorsa, `app.py` dosyasındaki port numarasını değiştirin:

```python
app.run(host='0.0.0.0', port=8080, debug=True)  # 8080'e değiştirdik
```

### Yayın başlamıyor

1. YouTube Stream Key'inizin doğru olduğundan emin olun
2. İnternet bağlantınızı kontrol edin
3. YouTube Studio'da canlı yayın ayarlarınızın aktif olduğunu doğrulayın
4. Firewall veya güvenlik duvarı FFmpeg'in internete erişimini engelliyor olabilir

### Yüksek CPU kullanımı

1. Daha düşük kalite seçin (1080p yerine 720p)
2. `app.py` içinde FFmpeg preset'ini değiştirin:
   - `veryfast` → `ultrafast` (düşük kalite, düşük CPU)
   - `veryfast` → `fast` (yüksek kalite, yüksek CPU)

## Gelişmiş Yapılandırma

### FFmpeg Parametrelerini Özelleştirme

`app.py` dosyasındaki `start_m3u8()` veya `start_file()` fonksiyonlarında FFmpeg parametrelerini özelleştirebilirsiniz:

```python
cmd = [
    'ffmpeg',
    '-re',
    '-i', source,
    '-c:v', 'libx264',
    '-preset', 'veryfast',  # ultrafast, superfast, veryfast, faster, fast, medium
    '-b:v', '4500k',
    '-maxrate', '4500k',
    '-bufsize', '9000k',
    '-pix_fmt', 'yuv420p',
    '-g', '50',
    '-c:a', 'aac',
    '-b:a', '128k',
    '-ar', '44100',
    '-f', 'flv',
    rtmp_url
]
```

### Maksimum Dosya Boyutu

`app.py` içinde maksimum upload boyutunu değiştirebilirsiniz:

```python
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB
```

## Güvenlik Notları

⚠️ **Önemli Güvenlik Uyarıları:**

1. **Stream Key'i Paylaşmayın**: YouTube stream anahtarınızı kimseyle paylaşmayın
2. **HTTPS Kullanın**: Üretim ortamında HTTPS kullanın (Nginx + Let's Encrypt)
3. **Güvenlik Duvarı**: Uygulamaya sadece güvenilir IP'lerden erişim verin
4. **Kimlik Doğrulama**: Üretimde mutlaka kullanıcı kimlik doğrulama ekleyin

## Lisans

MIT License - Kullanmakta özgürsünüz!

## Katkıda Bulunma

Katkılarınızı bekliyoruz! Pull request göndermekten çekinmeyin.

## Destek

Sorun yaşıyorsanız veya öneriniz varsa issue açabilirsiniz.

---

**Not**: Bu uygulama eğitim amaçlıdır. Üretim ortamında kullanmadan önce güvenlik önlemlerini almayı unutmayın.

Mutlu yayınlar! 🎥✨
