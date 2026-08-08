"""Konfigurasi dari environment variable. Tidak ada dependency luar."""

import os


def _int(name, default):
    raw = os.environ.get(name, "").strip()
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool(name, default):
    raw = os.environ.get(name, "").strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "on")


def _list(name):
    raw = os.environ.get(name, "")
    out = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            out.append(part)
    return out


# ---------------------------------------------------------------- Telegram
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
STORE_NAME = os.environ.get("STORE_NAME", "VPS Store").strip()
SUPPORT_CONTACT = os.environ.get("SUPPORT_CONTACT", "").strip()

ADMIN_IDS = set()
for _raw in _list("ADMIN_IDS"):
    try:
        ADMIN_IDS.add(int(_raw))
    except ValueError:
        pass

# ---------------------------------------------------------------- Database
DB_PATH = os.environ.get("DB_PATH", "vps.db").strip()

# ---------------------------------------------------------------- Pembayaran
PAKASIR_BASE_URL = os.environ.get("PAKASIR_BASE_URL", "https://app.pakasir.com").rstrip("/")
PAKASIR_PROJECT = os.environ.get("PAKASIR_PROJECT", "").strip()
PAKASIR_API_KEY = os.environ.get("PAKASIR_API_KEY", "").strip()

ORDER_EXPIRE_MINUTES = _int("ORDER_EXPIRE_MINUTES", 35)
CHECK_INTERVAL_SECONDS = _int("CHECK_INTERVAL_SECONDS", 15)
WEBHOOK_PORT = _int("WEBHOOK_PORT", 0)  # 0 = matikan webhook, andalkan polling

# ---------------------------------------------------------------- Siklus layanan
# Setelah expired VPS langsung di-stop. Data masih disimpan selama masa tenggang,
# jadi pelanggan yang telat bayar tidak langsung kehilangan datanya.
GRACE_DAYS = _int("GRACE_DAYS", 3)
AUTO_DESTROY = _bool("AUTO_DESTROY", True)
REMIND_BEFORE_HOURS = [72, 24, 6]
STUCK_PROVISION_SECONDS = _int("STUCK_PROVISION_SECONDS", 600)
MAX_PROVISION_ATTEMPTS = _int("MAX_PROVISION_ATTEMPTS", 3)

# ---------------------------------------------------------------- Provider
# mock  = simulasi, aman buat tes tanpa server sungguhan
# proxmox = Proxmox VE via API token
PROVIDER = os.environ.get("PROVIDER", "mock").strip().lower()
MOCK_STATE_PATH = os.environ.get("MOCK_STATE_PATH", "mock_provider.json").strip()

PROXMOX_HOST = os.environ.get("PROXMOX_HOST", "").strip().rstrip("/")
PROXMOX_NODE = os.environ.get("PROXMOX_NODE", "pve").strip()
PROXMOX_TOKEN = os.environ.get("PROXMOX_TOKEN", "").strip()
PROXMOX_VERIFY_SSL = _bool("PROXMOX_VERIFY_SSL", False)
PROXMOX_KIND = os.environ.get("PROXMOX_KIND", "qemu").strip().lower()
PROXMOX_STORAGE = os.environ.get("PROXMOX_STORAGE", "local-lvm").strip()
PROXMOX_DISK = os.environ.get("PROXMOX_DISK", "scsi0").strip()
PROXMOX_BRIDGE = os.environ.get("PROXMOX_BRIDGE", "vmbr0").strip()
PROXMOX_GATEWAY = os.environ.get("PROXMOX_GATEWAY", "").strip()
PROXMOX_NETMASK = _int("PROXMOX_NETMASK", 24)
PROXMOX_FULL_CLONE = _bool("PROXMOX_FULL_CLONE", True)
PROXMOX_TASK_TIMEOUT = _int("PROXMOX_TASK_TIMEOUT", 300)

# ---------------------------------------------------------------- Jaringan VPS
# pool = ambil IP dari daftar IP_POOL, dipesan per layanan (deterministik)
# dhcp = biarkan VPS ambil IP sendiri, bot menanyakan ke guest agent
IP_MODE = os.environ.get("IP_MODE", "pool").strip().lower()
IP_POOL = _list("IP_POOL")
VM_USER = os.environ.get("VM_USER", "root").strip()
SSH_PORT = _int("SSH_PORT", 22)


def missing():
    """Cek konfigurasi wajib sebelum bot jalan."""
    problems = []
    if not BOT_TOKEN:
        problems.append("BOT_TOKEN belum diisi")
    if not ADMIN_IDS:
        problems.append("ADMIN_IDS belum diisi")
    if not PAKASIR_PROJECT or not PAKASIR_API_KEY:
        problems.append("PAKASIR_PROJECT / PAKASIR_API_KEY belum diisi")
    if PROVIDER == "proxmox":
        if not PROXMOX_HOST:
            problems.append("PROXMOX_HOST belum diisi")
        if not PROXMOX_TOKEN:
            problems.append("PROXMOX_TOKEN belum diisi")
        if IP_MODE == "pool" and not IP_POOL:
            problems.append("IP_MODE=pool tapi IP_POOL kosong")
        if IP_MODE == "pool" and not PROXMOX_GATEWAY:
            problems.append("IP_MODE=pool butuh PROXMOX_GATEWAY")
    return problems
