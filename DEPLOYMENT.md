# Fight Detection — Subpath Deployment Rehberi

`https://yzdd-gpu02.gop.edu.tr/fight-detection/` altında, mevcut projeye dokunmadan deploy.

---

## 1. Ön Gereksinimler

Sunucuda (Ubuntu) şunlar kurulu olmalı:

```bash
docker --version
docker compose version
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

Son komut GPU bilgisini ekrana basmalı. Basmıyorsa `nvidia-container-toolkit` eksik:

```bash
sudo apt-get install -y nvidia-container-toolkit
sudo systemctl restart docker
```

Mevcut nginx kurulu ve `https://yzdd-gpu02.gop.edu.tr/` 443'te çalışıyor olmalı.

---

## 2. Repo'yu Yükle

```bash
sudo mkdir -p /opt/fight-detection
sudo chown -R "$USER":"$USER" /opt/fight-detection
cd /opt/fight-detection

git clone <REPO_URL> .
# veya rsync ile:
# rsync -avz --exclude='.git' --exclude='staticfiles' --exclude='media' \
#   ./ user@yzdd-gpu02:/opt/fight-detection/
```

---

## 3. `.env` Oluştur

```bash
cd /opt/fight-detection
cp .env.example .env

SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')
PGPASS=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')

sed -i "s|DEGISTIR_SECRET_KEY|$SECRET|g" .env
sed -i "s|DEGISTIR_GUCLU_PAROLA|$PGPASS|g" .env

chmod 600 .env
```

`.env` içeriğini doğrula:

```bash
grep -E '^(URL_PREFIX|DJANGO_DEBUG|CSRF_TRUSTED_ORIGINS|FRONTEND_URL)=' .env
```

Beklenen çıktı:

```
URL_PREFIX=/fight-detection
DJANGO_DEBUG=0
CSRF_TRUSTED_ORIGINS=https://yzdd-gpu02.gop.edu.tr
FRONTEND_URL=https://yzdd-gpu02.gop.edu.tr/fight-detection
```

---

## 4. Build & Up

```bash
cd /opt/fight-detection
docker compose build
docker compose up -d
docker compose logs -f app
```

Loglarda şunu gör:

```
Listening at: http://0.0.0.0:8000
Booting worker with pid: ...
```

`Ctrl+C` ile log'dan çık (servis arka planda çalışmaya devam eder).

---

## 5. `collectstatic` ve Süperuser

`collectstatic` zaten container start'ta çalışıyor. Tekrar çalıştırmak istersen:

```bash
docker compose exec app python Fight_backend_project/backend_frontend_project/manage.py collectstatic --noinput
```

Süperuser oluştur:

```bash
docker compose exec app python Fight_backend_project/backend_frontend_project/manage.py createsuperuser
```

---

## 6. Static / Media İzinleri

nginx, Docker user'ı dışında bir kullanıcıyla çalışır. `alias` path'lerini okuyabilmesi için:

```bash
sudo chmod -R o+rX /opt/fight-detection/Fight_backend_project/backend_frontend_project/staticfiles
sudo chmod -R o+rX /opt/fight-detection/Fight_backend_project/backend_frontend_project/media
```

`media` Docker volume'una mount edildiyse host path'i bul:

```bash
docker volume inspect fight-detection_media_data | grep Mountpoint
```

Çıktıdaki path için de `chmod -R o+rX` uygula. Alternatif: `nginx-fight-detection.conf`'taki `alias` satırını volume mountpoint'ine yönlendir.

---

## 7. Nginx Config'e Snippet Ekle

Mevcut nginx config dosyasını bul:

```bash
sudo nginx -T 2>/dev/null | grep -E 'configuration file' | head -5
# veya
sudo grep -rl 'yzdd-gpu02.gop.edu.tr' /etc/nginx/
```

Domain için `server { listen 443 ssl; server_name yzdd-gpu02.gop.edu.tr; ... }` bloğunun **içine** `nginx-fight-detection.conf` içeriğini yapıştır. Mevcut `location /` veya başka bloklara dokunma.

Test ve reload:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

---

## 8. Test URL'leri

```bash
# 1. Login redirect (302 → /fight-detection/accounts/splash/?next=...)
curl -kI https://yzdd-gpu02.gop.edu.tr/fight-detection/

# 2. Static dosya
curl -kI https://yzdd-gpu02.gop.edu.tr/fight-detection/static/dashboard/dashboard.js

# 3. Mevcut proje etkilenmemiş
curl -kI https://yzdd-gpu02.gop.edu.tr/

# 4. Portlar dışarı kapalı (iki komut da "filtered" veya "closed" dönmeli)
nmap -p 5432,5433,8000,8001 yzdd-gpu02.gop.edu.tr
```

Tarayıcı testi:

1. `https://yzdd-gpu02.gop.edu.tr/fight-detection/` → splash/login
2. Login → `/fight-detection/dashboard/`
3. DevTools > Network: tüm CSS/JS `/fight-detection/static/...` URL'leri 200 dönüyor
4. DevTools > Application > Cookies: `fight_sessionid`, `fight_csrftoken` → `Path=/fight-detection/`
5. Dashboard "Start" butonu → 200 cevap, badge "Sistem Aktif"
6. `/fight-detection/admin/` → admin login
7. Mevcut projeden hâlâ login olabiliyor musun? Cookie isimleri farklı olduğu için iki proje birbirinin oturumuna karışmamalı.

---

## 9. Sorun Giderme

| Belirti | Olası Sebep | Çözüm |
| --- | --- | --- |
| `CSRF verification failed` (login form) | `CSRF_TRUSTED_ORIGINS` `.env`'de eksik | `.env`'e `https://yzdd-gpu02.gop.edu.tr` ekle, `docker compose restart app` |
| `502 Bad Gateway` | Gunicorn çalışmıyor veya `8001` dinlemiyor | `docker compose ps`, `docker compose logs app`, `ss -lntp \| grep 8001` |
| Login sonrası tekrar login sayfası (loop) | Cookie path yanlış set edildi veya iki proje aynı cookie ismini kullanıyor | DevTools > Cookies'de `fight_sessionid` görünüyor mu? `Path=/fight-detection/` mi? Yanlışsa `settings.py`'da `SESSION_COOKIE_PATH` ve `SESSION_COOKIE_NAME`'i kontrol et |
| Static 404 | `collectstatic` çalışmadı veya nginx alias path yanlış | `ls /opt/fight-detection/Fight_backend_project/backend_frontend_project/staticfiles` boş mu? `docker compose exec app python .../manage.py collectstatic --noinput`; nginx `alias` path'i kontrol et |
| Static 403 | nginx kullanıcısı path'i okuyamıyor | `chmod -R o+rX /opt/fight-detection/Fight_backend_project/backend_frontend_project/staticfiles` |
| `nvidia-smi` container'da çalışmıyor | nvidia-container-toolkit eksik | `sudo apt-get install -y nvidia-container-toolkit && sudo systemctl restart docker` |
| Postgres bağlanamıyor (`could not connect to server`) | Mevcut sunucuda 5432 kullanılıyor | `docker-compose.yml`'de host port `5433`'e çekildi; container içi hâlâ `5432`. `.env`'de `POSTGRES_PORT=5432` (container içi) doğru olmalı |
| `8001` çakışması | Başka servis dinliyor | `sudo ss -lntp \| grep 8001`, çakışırsa `docker-compose.yml`'de `8002:8000`'e çek + `nginx-fight-detection.conf`'ta `proxy_pass`'i güncelle |
| Dashboard'da "Sistem Aktif" değişmiyor | JS fetch URL'i prefix'siz kalmış olabilir | DevTools > Network, `/dashboard/status/` 404 mü, `/fight-detection/dashboard/status/` 200 mü? |
| Mevcut projenin login'i bozuldu | Cookie path/name çakışması | Cookie isimleri `fight_*` prefix'li olmalı; tarayıcıdaki eski cookie'leri temizle |
| Admin redirect `/admin/`'e gidiyor (prefix'siz) | Eski middleware/views patch'lenmedi | `accounts/middleware.py`, `accounts/views.py` ve `urls.py` patch'lerini doğrula |

---

## 10. Güncelleme

```bash
cd /opt/fight-detection
git pull
docker compose build app
docker compose up -d
docker compose exec app python Fight_backend_project/backend_frontend_project/manage.py migrate
docker compose exec app python Fight_backend_project/backend_frontend_project/manage.py collectstatic --noinput
docker compose restart app
```
