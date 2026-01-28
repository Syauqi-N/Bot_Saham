# Bot Saham WhatsApp (IDX)

Bot WhatsApp berbasis WAHA untuk cek harga saham IDX secara near-realtime lewat TradingView (tvDatafeed).

## Fitur
- $KODE (contoh: $BBCA)
- !ihsg
- !help
- Cache dan rate limit sederhana

## Prasyarat
- Python 3.10+
- WAHA (self-hosted WhatsApp API)
- Akun TradingView (disarankan untuk stabilitas)

## Tutorial Cepat (10 Menit)
### 1) Siapkan bot
```bash
cd ~/Documents/bot_saham2
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Isi kredensial TradingView di `.env` (opsional tapi disarankan):
```
TRADINGVIEW_USERNAME=...
TRADINGVIEW_PASSWORD=...
```

Jalankan bot:
```bash
python bot_saham.py
```

### 2) Jalankan WAHA (Docker)
Contoh minimal:
```bash
docker run -d --name backend-waha-1 -p 3000:3000 devlikeapro/waha:latest
```

Login WhatsApp di WAHA dashboard:
```
http://localhost:3000/dashboard
```
Pastikan session status **WORKING**.

### 3) Set webhook WAHA
Karena WAHA berjalan di Docker, URL webhook harus mengarah ke host.

Opsi umum:
- `http://host.docker.internal:5000/webhook`
- `http://172.17.0.1:5000/webhook` (Linux docker bridge)

Di dashboard WAHA:
1) Pilih session `default`
2) Bagian **Webhooks** → **+Webhook**
3) URL: salah satu URL di atas
4) Events: `message` (atau `message.any`)
5) Save/Update

Jika muncul **Unauthorized**, masukkan API key WAHA di dashboard (atau matikan auth di container).

### 4) Test lokal
```bash
python scripts/simulate_webhook.py --text '$BBCA'
```

### 5) Test dari WhatsApp
Kirim pesan ke nomor WAHA:
- `$BBCA`
- `!ihsg`
- `!help`

## Contoh Output
```
BBCA (IDX)
Close: 7,525
Change: -25 (-0.33%)
O/H/L: 7,550 / 7,550 / 7,525
Volume: 146,800

📊 SUPPORT & RESISTANCE — BBCA (1 Day)

🔻 Support
S1: 7,450
S2: 7,200
S3: 6,980

🔺 Resistance
R1: 7,820
R2: 8,050
R3: 8,320

⏱ 2026-01-27 00:00:00
© Haris Stockbit
```

## Setup
1) Buat virtualenv dan install dependencies:
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2) Salin env file:
```bash
cp .env.example .env
```

3) Isi kredensial TradingView di `.env` jika ada:
```
TRADINGVIEW_USERNAME=...
TRADINGVIEW_PASSWORD=...
```

4) Jalankan bot:
```bash
python bot_saham.py
```

## WAHA
Jalankan WAHA dan pastikan sesi WhatsApp aktif. Set webhook ke:
```
http://<host>:5000/webhook
```

Untuk dev, gunakan ngrok dan set webhook ke URL publik ngrok.

## Usage
- `$BBCA` → quote saham + support/resistance 1D
- `!ihsg` → indeks IHSG
- `!help` → daftar perintah

## Tips Webhook (Docker)
Jika bot berjalan di host dan WAHA di Docker, gunakan:
- `http://host.docker.internal:5000/webhook`
- atau `http://172.17.0.1:5000/webhook`

Untuk mengecek dari container:
```bash
docker exec -it backend-waha-1 curl -s http://172.17.0.1:5000/health
```

## Konfigurasi penting
- `WAHA_BASE_URL` default `http://localhost:3000`
- `WAHA_SESSION` default `default`
- `TV_INTERVAL` default `1d`
- `IHSG_SYMBOL` default `COMPOSITE` (ubah jika simbol IHSG berbeda)

## Troubleshooting
- Jika sering gagal, pastikan kredensial TradingView benar.
- Jika data kosong, cek simbol saham dan coba ulang beberapa saat lagi.
- Alternatif fallback: gunakan `yahooquery` jika TradingView sering error.
- Jika WAHA tidak bisa POST ke bot, pastikan URL webhook tidak memakai `localhost`
- Jika muncul `No LID for user`, pastikan chat asli mengirim pesan ke WAHA dulu
