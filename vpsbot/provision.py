"""Pemenuhan order: bikin VPS baru atau perpanjang yang sudah ada.

Dipakai bareng oleh webhook dan poller. Aman dipanggil berkali-kali karena
claim_provision() memastikan hanya satu pemanggil yang benar-benar bekerja.
"""

import secrets
import string
import time

from . import config, db, tg
from .providers import ProvisionError, ProvisionFatal, Spec

_PW_ALPHABET = string.ascii_letters + string.digits + "!@#%^*_-+="


def gen_password(length=18):
    return "".join(secrets.choice(_PW_ALPHABET) for _ in range(length))


def new_order_id():
    stamp = time.strftime("%y%m%d%H%M%S", time.gmtime())
    return "VPS" + stamp + secrets.token_hex(2).upper()


def rupiah(amount):
    return "Rp" + format(int(amount), ",d").replace(",", ".")


def _days(seconds):
    return max(0, int(seconds // 86400))


def fmt_expiry(ts):
    return time.strftime("%d %b %Y %H:%M", time.localtime(ts)) + " WIB"


# ---------------------------------------------------------------- credentials
def credentials_text(service, plan=None):
    lines = [
        "<b>VPS #" + str(service["id"]) + " siap dipakai</b>",
        "",
        "<b>IP</b>: <code>" + str(service["ip"] or "-") + "</code>",
        "<b>User</b>: <code>" + str(service["username"] or "root") + "</code>",
        "<b>Password</b>: <code>" + str(service["password"] or "-") + "</code>",
        "<b>Port SSH</b>: <code>" + str(service["ssh_port"] or 22) + "</code>",
    ]
    if plan is not None:
        lines.insert(2, "<b>Paket</b>: " + tg.escape(plan["name"]))
        lines.insert(
            3,
            "<b>Spek</b>: " + str(plan["cpu"]) + " vCPU / "
            + str(plan["ram_mb"]) + " MB RAM / " + str(plan["disk_gb"]) + " GB disk",
        )
    lines.append("<b>Aktif sampai</b>: " + fmt_expiry(service["expires_at"]))
    lines.append("")
    lines.append("Login: <code>ssh " + str(service["username"] or "root")
                 + "@" + str(service["ip"] or "IP") + "</code>")
    lines.append("")
    lines.append("Ganti password setelah login pertama.")
    return "\n".join(lines)


# ---------------------------------------------------------------- fulfillment
def fulfill(order_id, provider, now=None):
    """Kerjakan order yang sudah dibayar. True kalau order ini selesai olehmu."""
    now = now or int(time.time())

    order = db.get_order(order_id)
    if order is None:
        return False

    # kunci: cuma satu pemanggil yang lolos
    if not db.claim_provision(order_id, now):
        return False

    order = db.get_order(order_id)  # ambil ulang untuk baca attempts terbaru

    try:
        if order["kind"] == "renew":
            service_id = _renew(order, provider, now)
        else:
            service_id = _provision_new(order, provider, now)
        db.finish_order(order_id, service_id, now)
        db.log_event("order_done", order["kind"], order_id, service_id, now)
        return True

    except ProvisionFatal as exc:
        _fail(order, exc, now, permanent=True)
        return False

    except Exception as exc:  # termasuk ProvisionError
        attempts = order["attempts"] or 1
        if attempts >= config.MAX_PROVISION_ATTEMPTS:
            _fail(order, exc, now, permanent=True)
        else:
            # kembalikan ke 'paid' supaya tick berikutnya mencoba lagi
            db.release_provision(order["id"], exc)
            db.log_event("provision_retry", str(exc), order["id"], None, now)
        return False


def _fail(order, error, now, permanent=False):
    db.fail_order(order["id"], error, now)
    db.log_event("order_failed", str(error), order["id"], order["service_id"], now)

    service_id = order["service_id"]
    if service_id:
        db.set_service_status(service_id, "error", now, str(error)[:300])
        # lepas IP-nya supaya tidak nyangkut di layanan yang gagal
        db.free_ip(service_id)

    tg.send(
        order["tg_id"],
        "<b>Pesanan bermasalah</b>\n\nOrder <code>" + order["id"] + "</code> sudah "
        "dibayar tapi VPS gagal disiapkan. Pembayaranmu tercatat dan admin sudah "
        "diberi tahu, jadi uangmu tidak hilang.\n\nAdmin akan menindaklanjuti "
        "secara manual.",
    )
    tg.notify_admins(
        "PERLU TINDAKAN\n\nOrder <code>" + order["id"] + "</code> gagal.\n"
        "User: <code>" + str(order["tg_id"]) + "</code>\n"
        "Paket: " + str(order["plan_id"]) + "\n"
        "Jumlah: " + rupiah(order["amount"]) + "\n"
        "Error: <code>" + tg.escape(str(error)[:300]) + "</code>"
    )


def _provision_new(order, provider, now):
    plan = db.get_plan(order["plan_id"])
    if plan is None:
        raise ProvisionFatal("Paket " + str(order["plan_id"]) + " sudah tidak ada")

    expires_at = now + int(plan["period_days"]) * 86400

    # kalau ini percobaan ulang, pakai baris layanan yang sudah ada
    service_id = order["service_id"]
    if service_id:
        existing = db.get_service(service_id)
        if existing is None:
            service_id = None

    if not service_id:
        service_id = db.create_service(
            order["tg_id"], plan["id"], provider.name, expires_at, now
        )
        db.set_order_service(order["id"], service_id)

    service = db.get_service(service_id)

    # alokasi IP hanya sekali, walau order diulang
    ip = service["ip"]
    if config.IP_MODE == "pool" and not ip:
        ip = db.alloc_ip(service_id)
        if ip is None:
            tg.notify_admins("STOK IP HABIS - order " + order["id"] + " tertahan")
            raise ProvisionFatal("Stok IP habis")

    password = service["password"] or gen_password()
    hostname = service["hostname"] or ("vps" + str(service_id))

    db.attach_service_details(
        service_id, hostname=hostname, ip=ip, username=config.VM_USER,
        password=password, ssh_port=config.SSH_PORT,
    )

    spec = Spec(
        service_id=service_id,
        hostname=hostname,
        cpu=int(plan["cpu"]),
        ram_mb=int(plan["ram_mb"]),
        disk_gb=int(plan["disk_gb"]),
        username=config.VM_USER,
        password=password,
        ip=ip,
        gateway=config.PROXMOX_GATEWAY or None,
        netmask=config.PROXMOX_NETMASK,
        template_vmid=plan["template_vmid"],
    )

    ref = provider.create(spec)

    db.attach_service_details(
        service_id,
        node=ref.get("node"),
        vmid=ref.get("vmid"),
        ip=ref.get("ip") or ip,
        username=ref.get("username") or config.VM_USER,
        password=ref.get("password") or password,
        ssh_port=ref.get("ssh_port") or config.SSH_PORT,
    )
    db.set_expiry(service_id, expires_at)
    db.set_service_status(service_id, "active", now)
    db.log_event("service_created", "vmid=" + str(ref.get("vmid")),
                 order["id"], service_id, now)

    service = db.get_service(service_id)
    tg.send(order["tg_id"], credentials_text(service, plan))
    return service_id


def _renew(order, provider, now):
    service_id = order["service_id"]
    service = db.get_service(service_id) if service_id else None
    if service is None:
        raise ProvisionFatal("Layanan yang mau diperpanjang tidak ditemukan")

    plan = db.get_plan(order["plan_id"]) or db.get_plan(service["plan_id"])
    if plan is None:
        raise ProvisionFatal("Paket untuk perpanjangan tidak ada")

    # kalau belum expired, sisa waktu tidak hilang
    base = max(now, int(service["expires_at"]))
    new_expiry = base + int(plan["period_days"]) * 86400
    db.set_expiry(service_id, new_expiry)

    was_off = service["status"] in ("suspended", "suspending", "error")
    if was_off:
        provider.start(service)
    db.set_service_status(service_id, "active", now)
    db.log_event("service_renewed", "sampai " + str(new_expiry),
                 order["id"], service_id, now)

    note = "VPS dinyalakan kembali." if was_off else "VPS tetap berjalan."
    tg.send(
        order["tg_id"],
        "<b>Perpanjangan berhasil</b>\n\nVPS #" + str(service_id) + " aktif sampai "
        + fmt_expiry(new_expiry) + ".\n" + note,
    )
    return service_id
