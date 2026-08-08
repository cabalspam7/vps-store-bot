# VPS Store Bot

Bot Telegram untuk jualan VPS. Pembayaran QRIS, VPS dibuat otomatis setelah
lunas, dan **saat masa aktif habis VPS otomatis distop** — lanjut dihapus
setelah masa tenggang kalau tetap tidak diperpanjang.

Tanpa dependency eksternal. Python 3.8+ standard library saja.

---

## Alur lengkap

```
pelanggan pilih paket
        |
   tagihan QRIS  ---- tidak dibayar 35 menit ----> order kedaluwarsa
        |
   pembayaran lunas (webhook / polling)
        |
   VPS dibuat otomatis -> data login dikirim ke chat
        |
   aktif 30 hari
        |
   peringatan otomatis: 3 hari, 1 hari, 6 jam sebelum habis
        |
   EXPIRED -> VPS otomatis di-STOP (data masih utuh)
        |
   +---- diperpanjang -> VPS nyala lagi, data & IP sama
   |
   +---- didiamkan 3 hari -> VPS + disk dihapus permanen, IP balik ke pool
```

---

## Cara jalan (5 langkah)

### 1. Siapkan file konfigurasi

```bash
cp .env.example .env
nano .env
```

Wajib diisi: `BOT_TOKEN`, `ADMIN_IDS`, `PAKASIR_PROJECT`, `PAKASIR_API_KEY`.
Bot menolak jalan kalau ada yang kosong, bukan diam-diam error di tengah jalan.

### 2. Coba dulu pakai mode simulasi

Biarkan `PROVIDER=mock`. Mode ini menjalankan seluruh alur — order, bayar,
provisioning, expiry, suspend, hapus — tanpa menyentuh server sungguhan.
Tes semua tombol dulu di sini sebelum menyambung ke Proxmox.

```bash
set -a; source .env; set +a
python3 bot.py
```

### 3. Sambung ke Proxmox

Ubah `PROVIDER=proxmox` dan isi bagian Proxmox di `.env`. Lihat bagian
[Persiapan Proxmox](#persiapan-proxmox) di bawah.

### 4. Atur paket jualanmu

Di Telegram, sebagai admin:

```
/addplan starter|VPS Starter|1|1024|20|25000|30|9001|Ubuntu 22.04
/addplan basic|VPS Basic|2|2048|40|45000|30|9001|Ubuntu 22.04
/addplan pro|VPS Pro|4|4096|80|85000|30|9001|Ubuntu 22.04
```

Format: `id|nama|cpu|ram_mb|disk_gb|harga|hari|template_vmid|os`

`template_vmid` = VMID template Proxmox yang mau dicloning.

### 5. Jalankan permanen

```bash
docker compose up -d
docker compose logs -f
```

Atau pakai systemd:

```ini
[Unit]
Description=VPS Store Bot
After=network-online.target

[Service]
WorkingDirectory=/opt/vps-bot
EnvironmentFile=/opt/vps-bot/.env
ExecStart=/usr/bin/python3 /opt/vps-bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## Perintah

### Pelanggan

| Perintah | Fungsi |
|---|---|
| `/start` | menu utama |
| `/plans` | daftar paket |
| `/myvps` | VPS milik sendiri + tombol perpanjang + lihat data login |
| `/orders` | riwayat pesanan |
| `/kupon KODE` | pasang kode diskon (`/kupon hapus` untuk melepas) |
| `/help` | penjelasan alur |

### Admin

| Perintah | Fungsi |
|---|---|
| `/stats` | pengguna, VPS aktif, pendapatan, pemakaian IP |
| `/addplan ...` | tambah / ubah paket |
| `/delplan id` | sembunyikan paket |
| `/allplans` | semua paket termasuk yang nonaktif |
| `/extend <id> <hari>` | tambah masa aktif manual (sekaligus nyalakan kalau mati) |
| `/stopvps <id>` | stop VPS sekarang |
| `/startvps <id>` | nyalakan VPS |
| `/addip ip1,ip2` | tambah IP ke pool |
| `/events` | 15 kejadian terakhir |
| `/addkupon KODE\|persen\|maks_pakai\|berlaku_hari` | bikin / ubah kupon |
| `/delkupon KODE` | matikan kupon |
| `/kupons` | daftar kupon + jumlah pemakaian |
| `/reseller <tg_id> <persen>` | diskon permanen; `0` mencabut |
| `/resellers` | daftar reseller |

---

## Diskon & reseller

Dua cara memberi harga khusus, dengan satu mesin perhitungan yang sama.

**Kupon** dibuat admin, dipasang sendiri oleh pelanggan:

```
/addkupon LAUNCH30|30|50|7
```

Diskon 30%, kuota 50 pemakaian, berlaku 7 hari. Isi `0` pada kuota atau masa
berlaku untuk tanpa batas. Pelanggan mengetik `/kupon LAUNCH30`, lalu harga
coret langsung muncul di detail paket dan di tagihan.

**Reseller** tidak perlu kode. Diskonnya menempel di akun dan otomatis berlaku
untuk semua pembelian *dan* perpanjangan:

```
/reseller 123456789 25
```

Aturan yang dipegang:

- Diskon tidak ditumpuk. Kalau pelanggan punya diskon reseller dan kupon
  sekaligus, yang dipakai hanya yang paling besar. Harga akhir tidak pernah
  jatuh lebih dalam dari yang kamu niatkan.
- Kuota kupon dipesan lewat `UPDATE` bersyarat saat order dibuat, jadi kuota
  tidak bisa kebobolan walau dua pelanggan menekan Beli di detik yang sama.
- Kalau order dibatalkan atau hangus, slot kupon dikembalikan otomatis.
- Kupon dilepas dari akun begitu terpakai, jadi tidak diam-diam ikut ke
  pesanan berikutnya.
- Nominal dibulatkan turun ke ratusan rupiah supaya angka QRIS enak dibaca.
- Setiap order menyimpan harga normal, nilai diskon, dan kode kupon, jadi
  `/stats` bisa menunjukkan total diskon yang sudah kamu berikan.
- Batas persen 1-90, dijaga di sisi bot maupun database.

Database lama tetap bisa dipakai: kolom baru ditambahkan otomatis lewat
migrasi `ALTER TABLE` saat bot start.

---

## Persiapan Proxmox

### 1. Bikin template dengan cloud-init

Cloud-init yang membuat username dan password bisa disuntik otomatis. Tanpa ini,
bot tidak bisa mengirim data login ke pelanggan.

```bash
# di node Proxmox
wget https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img

qm create 9001 --name ubuntu-2204-template --memory 1024 --cores 1 --net0 virtio,bridge=vmbr0
qm importdisk 9001 jammy-server-cloudimg-amd64.img local-lvm
qm set 9001 --scsihw virtio-scsi-pci --scsi0 local-lvm:vm-9001-disk-0
qm set 9001 --ide2 local-lvm:cloudinit
qm set 9001 --boot c --bootdisk scsi0
qm set 9001 --serial0 socket --vga serial0
qm set 9001 --agent enabled=1
qm template 9001
```

`--agent enabled=1` wajib kalau `IP_MODE=dhcp`, karena IP dibaca dari guest agent.

### 2. Bikin API token

Jangan pakai password root. Di Proxmox:
**Datacenter -> Permissions -> API Tokens -> Add**

Beri role yang cukup lewat **Datacenter -> Permissions -> Add -> API Token
Permission** pada path `/`:

- `PVEVMAdmin` — clone, config, start, stop, hapus VM
- `PVEDatastoreUser` pada storage yang dipakai

Isi ke `.env`:

```
PROXMOX_TOKEN=root@pam!botvps=uuid-yang-muncul
```

Token hanya ditampilkan sekali saat dibuat.

### 3. Tentukan IP

**`IP_MODE=pool`** (disarankan): kamu isi daftar IP publik yang kamu punya. Bot
memesan satu IP per VPS dan mengembalikannya ke pool saat VPS dihapus. Butuh
`PROXMOX_GATEWAY`.

**`IP_MODE=dhcp`**: VPS ambil IP dari DHCP, bot menanyakannya ke guest agent.
Lebih gampang, tapi IP bisa berubah setelah restart.

---

## Yang bikin bot ini tahan banting

Bagian-bagian ini sengaja dibuat begini, bukan kebetulan:

**Satu pembayaran = satu VPS, dijamin.** Webhook dan poller bisa datang di
detik yang sama. Keduanya harus lewat `UPDATE ... WHERE status='pending'` dan
`WHERE status='paid'`; hanya yang `rowcount == 1` yang lanjut kerja. Jadi tidak
ada VPS dobel walau webhook dikirim ulang berkali-kali oleh gateway.

**Webhook cuma jalur cepat, bukan syarat.** Isi webhook tidak dipercaya —
status lunas selalu dicek ulang ke API Pakasir. Poller tetap jalan, jadi kalau
webhook tidak sampai (server down, firewall, dsb) order tetap diproses. Kamu
bahkan bisa jalan tanpa webhook sama sekali (`WEBHOOK_PORT=0`).

**Gagal provisioning tidak menghilangkan uang pelanggan.** Order dikembalikan ke
status `paid` untuk dicoba lagi (maksimal 3x). Kalau tetap gagal, kamu dapat
notifikasi berisi order id dan pesan error-nya, dan pelanggan diberi tahu bahwa
pembayarannya tercatat. Percobaan ulang memakai baris layanan yang sama, jadi
tidak pernah muncul VPS kedua untuk satu order.

**Stop gagal tidak berarti VPS dibiarkan hidup gratis.** Kalau `stop()` error,
status dikembalikan ke `active` supaya masuk antrean lagi di siklus berikutnya,
dan kamu dapat notifikasi. Ini penting: kegagalan yang didiamkan berarti
pelanggan yang sudah expired tetap memakai resource-mu.

**Proses mati di tengah jalan bisa pulih.** Order yang macet di status
`provisioning` lebih dari 10 menit dikembalikan ke antrean otomatis.

**Expired tidak langsung menghapus data.** VPS distop dulu, data ditahan
`GRACE_DAYS` hari. Perpanjang, dan VPS nyala lagi dengan disk, IP, dan data yang
sama. Penghapusan permanen baru terjadi setelah masa tenggang, dan bisa
dimatikan lewat `AUTO_DESTROY=false`.

**Perpanjangan sebelum expired tidak menghanguskan sisa waktu.** Perhitungannya
`max(sekarang, expired_lama) + periode`, jadi pelanggan yang bayar lebih awal
tidak dirugikan.

---

## Tes

```bash
python3 test_vps_bot.py
```

17 skenario, termasuk yang biasanya baru ketahuan di produksi: pembayaran dobel,
provisioning gagal berulang, stop yang error, proses macet, perpanjangan VPS yang
sudah mati, masa tenggang, dan pelanggan yang mencoba mengakses VPS orang lain.
Tiga terakhir menguji diskon: kupon terpakai sekali saja, kuota yang habis,
kupon kedaluwarsa dan nonaktif, pengembalian slot saat order batal atau hangus,
serta diskon reseller yang tidak bisa ditumpuk dengan kupon.

Tes yang sama dijalankan otomatis di GitHub Actions pada Python 3.9-3.12,
tanpa satu pun `pip install` - itu sekalian membuktikan janji "standard library
saja".

Waktu disuntik lewat parameter `now`, jadi skenario 30 hari selesai dalam
hitungan milidetik.

---

## Struktur

```
bot.py                   titik masuk: preflight, scheduler, long polling
vpsbot/
  config.py              konfigurasi dari environment + validasi awal
  db.py                  SQLite; kunci anti-dobel untuk uang & provisioning
  tg.py                  klien Telegram (urllib, tahan rate limit)
  pakasir.py             QRIS: bikin tagihan, cek status, verifikasi webhook
  handlers.py            perintah, tombol, dan panel admin
  pricing.py             perhitungan diskon kupon & reseller
  provision.py           jalur pemenuhan order: bikin VPS / perpanjang
  scheduler.py           cek bayar, peringatan, suspend, hapus
  webhook.py             penerima webhook opsional
  providers/
    base.py              kontrak driver (stop & destroy wajib idempotent)
    mock.py              simulasi untuk tes
    proxmox.py           Proxmox VE via API token
test_vps_bot.py          tes siklus hidup penuh
```

Ganti hypervisor nanti? Cukup tambah satu file di `providers/` yang mengikuti
`base.py`. Logika order, expiry, dan suspend tidak perlu disentuh.

---

## Catatan jujur

- **LXC** didukung untuk clone/start/stop/hapus dan pengaturan CPU/RAM. Resize
  disk pada LXC lebih ribet dari VM; kalau paketmu beda-beda ukuran disk,
  `PROXMOX_KIND=qemu` lebih aman.
- Proxmox hanya bisa **memperbesar** disk, tidak mengecilkan. Buat template
  dengan disk sekecil paket terkecilmu.
- Password VPS disimpan di database supaya pelanggan bisa melihatnya lagi lewat
  tombol "Lihat Data Login". Batasi akses file `data/vps.db` (`chmod 600`) dan
  sarankan pelanggan mengganti password setelah login pertama.
- Bot ini mengelola siklus jual-beli dan on/off VPS. Backup, monitoring uptime,
  dan mitigasi DDoS di luar cakupannya.
