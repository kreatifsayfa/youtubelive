# Dokploy Deploy Guide

Bu uygulama Dokploy'da Nixpacks kullanılarak deploy edilmek üzere yapılandırılmıştır.

## Dokploy Deploy Adımları

### 1. Repository'yi Push Edin

GitHub, GitLab veya başka bir Git servisine projenizi push edin:

```bash
git init
git add .
git commit -m "Initial commit: YouTube Live Streamer"
git remote add origin <your-repo-url>
git push -u origin main
```

### 2. Dokploy'da Yeni Proje Oluşturun

1. Dokploy paneline giriş yapın
2. **"Create Project"** veya **"Yeni Proje"** tıklayın
3. Proje ismi girin: `youtubelive`
4. **"Create"** tıklayın

### 3. Service Oluşturun (Nixpacks)

1. Proje içinde **"Create Service"** tıklayın
2. Service tipi olarak **"Nixpacks"** seçin
3. Aşağıdaki ayarları yapın:

| Ayar | Değer |
|------|-------|
| **Name** | `youtubelive` |
| **Repository** | `https://github.com/kullanici/repo.git` |
| **Branch** | `main` |
| **Build Path** | `/` (boş bırakın) |
| **Port** | `5000` |

### 4. Environment Variables (Opsiyonel)

Dokploy'da environment variables ekleyebilirsiniz:

| Variable | Value | Description |
|----------|-------|-------------|
| `PORT` | `5000` | Uygulama portu |
| `FLASK_DEBUG` | `false` | Production mode |
| `PYTHONUNBUFFERED` | `1` | Python output buffer |

### 5. Deploy Edin

**"Deploy"** butonuna tıklayın ve deploy işlemini bekleyin.

Nixpacks otomatik olarak:
- Python ortamını kuracak
- FFmpeg'i yükleyecek
- Gunicorn server'ı başlatacak

## Nixpacks Yapılandırması

`nixpacks.toml` dosyası deploy için gerekli tüm ayarları içerir:

```toml
[phases.setup]
nixPkgs = ["python3", "ffmpeg", "ffmpeg-full"]

[phases.install]
cmds = [
  "pip install --no-cache-dir -r requirements.txt",
  "pip install gunicorn"
]

[start]
cmd = "gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 600 app:app"
```

## Domain Ayarları

Deploy tamamlandıktan sonra:

1. Proje ayarlarından **"Domains"** sekmesine gidin
2. Kendi domain'inizi ekleyin veya Dokploy'un otomatik verdiği domain'i kullanın
3. SSL sertifikası otomatik olarak verilir (Let's Encrypt)

## Yayın Portları ve Güvenlik

Uygulama için gerekli portlar:

| Port | Açıklama |
|------|----------|
| 5000 | Uygulama HTTP portu |
| 1935 | RTMP (opsiyonel - doğrudan streaming için) |

**Güvenlik Notu:**
- Uygulamaya reverse proxy arkasında çalışın (Traefik/Nginx)
- Production'da mutlaka HTTPS kullanın
- Stream key'leri environment variable olarak saklayabilirsiniz

## Storage / Volume Ayarları

Yüklenen dosyaların container restart sonrası silinmemesi için volume ekleyin:

1. Service ayarlarından **"Volumes"** sekmesine gidin
2. Yeni volume ekleyin:

| Volume Path | Type |
|-------------|------|
| `/app/uploads` | `local` |

## Logs ve Monitoring

Deploy sonrası logları görüntülemek için:

1. Service detay sayfasına gidin
2. **"Logs"** sekmesine tıklayın
3. Real-time logları izleyin

## Troubleshooting

### Deploy Başarısız Olursa

1. **Nixpacks Loglarını Kontrol Edin**
   - Build loglarını inceleyin
   - FFmpeg kurulumunu kontrol edin

2. **Port Çakışması**
   - PORT değişkenini değiştirin
   - Dokploy panelinden port ayarını kontrol edin

3. **Memory/CPU Limiti**
   - Dokploy'da container limits ayarlarını kontrol edin
   - FFmpeg streaming için en az 512MB RAM önerilir

### FFmpeg Hataları

Eğer FFmpeg bulunamadı hatası alırsanız:

```toml
# nixpacks.toml'de FFmpeg paketini kontrol edin
[phases.setup]
nixPkgs = ["python3", "ffmpeg", "ffmpeg-full"]
```

### Application Başlamıyor

Gunicorn hatalarını kontrol edin:

```bash
# Logs sekmesinden şu hatayı arayın
# "Application startup failed"
```

Environment variable'ların doğru ayarlandığından emin olun.

## Deploy Sonrası Kontrol

Deploy başarılı olduktan sonra:

1. ✅ Uygulama URL'ine gidin
2. ✅ Ana sayfanın yüklendiğini doğrulayın
3. ✅ M3U8 URL ile test yayın yapın
4. ✅ YouTube Studio'da yayın geldiğini kontrol edin

## Update Process

Yeni versiyon deploy etmek için:

1. Kodunuzu GitHub'a push edin
2. Dokploy'da **"Redeploy"** butonuna tıklayın
3. Zero-downtime deploy yapılacaktır

## Backup

Yüklenen dosyaları backup almak için:

1. Volume'ları düzenli backup edin
2. Database kullanmıyorsunuz (in-memory file storage)
3. Yeni deploy'da uploads klasörü boş olacaktır (volume kullanın)

## Support

Sorun yaşarsanız:
- Dokploy loglarını kontrol edin
- Nixpacks documentation: https://nixpacks.com/docs
- GitHub issues açabilirsiniz

---

**Happy Streaming! 🎥**
