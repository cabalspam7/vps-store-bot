"""Perintah dan tombol Telegram."""

import time

from . import config, db, pakasir, provision, tg

_provider = None
MAX_PENDING_ORDERS = 3


def setup(provider):
    global _provider
    _provider = provider


def is_admin(tg_id):
    return tg_id in config.ADMIN_IDS


# ------------------------------------------------------------------ keyboards
def main_menu():
    return [
        [{"text": "Paket VPS", "callback_data": "plans"}],
        [{"text": "VPS Saya", "callback_data": "myvps"}],
        [{"text": "Bantuan", "callback_data": "help"}],
    ]


def back_button(target="menu"):
    return [{"text": "Kembali", "callback_data": target}]


# ------------------------------------------------------------------ views
def welcome_text():
    return (
        "<b>" + tg.escape(config.STORE_NAME) + "</b>\n\n"
        "Sewa VPS, bayar QRIS, aktif otomatis.\n\n"
        "Pilih menu di bawah untuk mulai."
    )


def plans_view():
    plans = db.list_plans()
    if not plans:
        return "Belum ada paket tersedia.", [back_button()]

    lines = ["<b>Paket VPS</b>", ""]
    keyboard = []
    for plan in plans:
        lines.append(
            "<b>" + tg.escape(plan["name"]) + "</b> - "
            + provision.rupiah(plan["price"]) + " / " + str(plan["period_days"]) + " hari"
        )
        lines.append(
            "   " + str(plan["cpu"]) + " vCPU, " + str(plan["ram_mb"]) + " MB RAM, "
            + str(plan["disk_gb"]) + " GB disk"
        )
        lines.append("")
        keyboard.append([{
            "text": plan["name"] + " - " + provision.rupiah(plan["price"]),
            "callback_data": "plan:" + plan["id"],
        }])

    keyboard.append(back_button())
    return "\n".join(lines), keyboard


def plan_detail_view(plan_id):
    plan = db.get_plan(plan_id)
    if plan is None or not plan["enabled"]:
        return "Paket tidak tersedia.", [back_button("plans")]

    text = (
        "<b>" + tg.escape(plan["name"]) + "</b>\n\n"
        "CPU: " + str(plan["cpu"]) + " vCPU\n"
        "RAM: " + str(plan["ram_mb"]) + " MB\n"
        "Disk: " + str(plan["disk_gb"]) + " GB\n"
        "OS: " + tg.escape(plan["os_label"] or "-") + "\n"
        "Masa aktif: " + str(plan["period_days"]) + " hari\n\n"
        "<b>Harga: " + provision.rupiah(plan["price"]) + "</b>\n\n"
        "Setelah pembayaran terkonfirmasi, VPS dibuat otomatis dan data login "
        "dikirim ke chat ini."
    )
    keyboard = [
        [{"text": "Beli Sekarang", "callback_data": "buy:" + plan["id"]}],
        back_button("plans"),
    ]
    return text, keyboard


def services_view(tg_id):
    services = db.list_services(tg_id)
    if not services:
        return (
            "Kamu belum punya VPS.\n\nLihat /plans untuk mulai.",
            [[{"text": "Lihat Paket", "callback_data": "plans"}], back_button()],
        )

    now = int(time.time())
    lines = ["<b>VPS Saya</b>", ""]
    keyboard = []
    labels = {
        "active": "Aktif",
        "suspended": "Dimatikan (expired)",
        "suspending": "Sedang dimatikan",
        "provisioning": "Sedang disiapkan",
        "terminating": "Sedang dihapus",
        "error": "Bermasalah",
    }

    for service in services:
        status = labels.get(service["status"], service["status"])
        lines.append("<b>VPS #" + str(service["id"]) + "</b> - " + status)
        lines.append("   IP: <code>" + str(service["ip"] or "-") + "</code>")
        if service["status"] == "active":
            remaining = int(service["expires_at"]) - now
            days = max(0, remaining // 86400)
            lines.append("   Sisa: " + str(days) + " hari")
        lines.append("")
        keyboard.append([{
            "text": "VPS #" + str(service["id"]) + " (" + status + ")",
            "callback_data": "svc:" + str(service["id"]),
        }])

    keyboard.append(back_button())
    return "\n".join(lines), keyboard


def service_detail_view(tg_id, service_id):
    service = db.get_service(service_id)
    if service is None or service["tg_id"] != tg_id:
        return "VPS tidak ditemukan.", [back_button("myvps")]

    plan = db.get_plan(service["plan_id"])
    now = int(time.time())

    lines = ["<b>VPS #" + str(service["id"]) + "</b>", ""]
    if plan is not None:
        lines.append("Paket: " + tg.escape(plan["name"]))
        lines.append(
            "Spek: " + str(plan["cpu"]) + " vCPU / " + str(plan["ram_mb"])
            + " MB / " + str(plan["disk_gb"]) + " GB"
        )
    lines.append("IP: <code>" + str(service["ip"] or "-") + "</code>")
    lines.append("Status: " + str(service["status"]))
    lines.append("Expired: " + provision.fmt_expiry(service["expires_at"]))

    if service["status"] == "suspended":
        deadline = int(service["suspended_at"] or now) + config.GRACE_DAYS * 86400
        left_days = max(0, (deadline - now) // 86400)
        lines.append("")
        lines.append(
            "VPS dimatikan karena expired. Data masih ada, sisa "
            + str(left_days) + " hari sebelum dihapus permanen."
        )

    keyboard = []
    if plan is not None:
        keyboard.append([{
            "text": "Perpanjang " + str(plan["period_days"]) + " hari - "
                    + provision.rupiah(plan["price"]),
            "callback_data": "renew:" + str(service["id"]),
        }])
    if service["password"]:
        keyboard.append([{
            "text": "Lihat Data Login",
            "callback_data": "cred:" + str(service["id"]),
        }])
    keyboard.append(back_button("myvps"))
    return "\n".join(lines), keyboard


def payment_view(order, plan, kind="new"):
    url = pakasir.checkout_url(order["id"], order["amount"])
    judul = "Perpanjangan" if kind == "renew" else "Pesanan Baru"
    text = (
        "<b>" + judul + "</b>\n\n"
        "Order: <code>" + order["id"] + "</code>\n"
        "Paket: " + tg.escape(plan["name"]) + "\n"
        "Total: <b>" + provision.rupiah(order["amount"]) + "</b>\n\n"
        "Bayar pakai QRIS lewat tombol di bawah. Setelah bayar, VPS diproses "
        "otomatis dalam beberapa detik.\n\n"
        "Order kedaluwarsa dalam " + str(config.ORDER_EXPIRE_MINUTES) + " menit."
    )
    keyboard = [
        [{"text": "Bayar QRIS", "url": url}],
        [{"text": "Saya Sudah Bayar", "callback_data": "check:" + order["id"]}],
        [{"text": "Batalkan", "callback_data": "cancel:" + order["id"]}],
    ]
    return text, keyboard


def help_text():
    lines = [
        "<b>Bantuan</b>",
        "",
        "/plans - lihat paket VPS",
        "/myvps - VPS milikmu &amp; perpanjangan",
        "/orders - riwayat pesanan",
        "",
        "<b>Cara kerja</b>",
        "1. Pilih paket lalu bayar QRIS",
        "2. VPS dibuat otomatis, data login dikirim ke chat ini",
        "3. Sebelum expired, bot mengingatkan 3 hari, 1 hari, dan 6 jam sebelumnya",
        "4. Saat expired, VPS otomatis dimatikan",
        "5. Data disimpan " + str(config.GRACE_DAYS) + " hari; perpanjang dan VPS nyala lagi",
    ]
    if config.SUPPORT_CONTACT:
        lines.append("")
        lines.append("Bantuan: " + tg.escape(config.SUPPORT_CONTACT))
    return "\n".join(lines)


# ------------------------------------------------------------------ orders
def _pending_count(tg_id):
    return len([
        o for o in db.list_user_orders(tg_id, 20) if o["status"] == "pending"
    ])


def create_order_for(tg_id, plan, kind="new", service_id=None, now=None):
    now = now or int(time.time())
    order_id = provision.new_order_id()
    expires_at = now + config.ORDER_EXPIRE_MINUTES * 60
    db.create_order(
        order_id, tg_id, plan["id"], kind, int(plan["price"]), expires_at,
        service_id, now,
    )
    db.log_event("order_created", kind, order_id, service_id, now)
    return db.get_order(order_id)


def check_payment_now(order_id, tg_id):
    """Dipanggil saat pelanggan menekan 'Saya Sudah Bayar'."""
    order = db.get_order(order_id)
    if order is None or order["tg_id"] != tg_id:
        return "Order tidak ditemukan."

    if order["status"] in ("done",):
        return "Order ini sudah selesai diproses."
    if order["status"] in ("paid", "provisioning"):
        return "Pembayaran diterima, VPS sedang disiapkan."
    if order["status"] == "expired":
        return "Order sudah kedaluwarsa. Silakan pesan lagi."
    if order["status"] == "canceled":
        return "Order sudah dibatalkan."
    if order["status"] == "failed":
        return "Order bermasalah, admin sedang menanganinya."

    try:
        paid = pakasir.is_paid(order["id"], order["amount"])
    except Exception as exc:
        print("[handlers] cek pembayaran gagal: " + str(exc))
        return "Belum bisa memeriksa pembayaran. Coba lagi sebentar lagi."

    if not paid:
        return "Pembayaran belum masuk. Kalau baru bayar, tunggu ~1 menit."

    if db.mark_paid(order["id"]):
        db.log_event("payment_confirmed", "tombol", order["id"])
    provision.fulfill(order["id"], _provider)
    return "Pembayaran diterima. VPS sedang disiapkan."


# ------------------------------------------------------------------ admin
def admin_help():
    return (
        "<b>Perintah Admin</b>\n\n"
        "/stats - ringkasan\n"
        "/addplan id|nama|cpu|ram_mb|disk_gb|harga|hari|template_vmid|os\n"
        "/delplan id - sembunyikan paket\n"
        "/allplans - semua paket termasuk nonaktif\n"
        "/extend &lt;service_id&gt; &lt;hari&gt; - tambah masa aktif manual\n"
        "/stopvps &lt;service_id&gt; - stop sekarang\n"
        "/startvps &lt;service_id&gt; - nyalakan &amp; set aktif\n"
        "/addip ip1,ip2 - tambah IP ke pool\n"
        "/events - 15 kejadian terakhir"
    )


def handle_admin(chat_id, tg_id, text):
    parts = text.split(maxsplit=1)
    command = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if command == "/stats":
        s = db.stats()
        tg.send(
            chat_id,
            "<b>Statistik</b>\n\n"
            "Pengguna: " + str(s["users"]) + "\n"
            "VPS aktif: " + str(s["active"]) + "\n"
            "Dimatikan: " + str(s["suspended"]) + "\n"
            "Dihapus: " + str(s["terminated"]) + "\n"
            "Order pending: " + str(s["pending_orders"]) + "\n"
            "Order gagal: " + str(s["failed_orders"]) + "\n"
            "IP terpakai: " + str(s["ip_used"]) + "/" + str(s["ip_total"]) + "\n"
            "Pendapatan: " + provision.rupiah(s["revenue"]),
        )
        return True

    if command == "/addplan":
        fields = [f.strip() for f in rest.split("|")]
        if len(fields) < 7:
            tg.send(chat_id, "Format:\n/addplan id|nama|cpu|ram_mb|disk_gb|harga|hari|template_vmid|os")
            return True
        try:
            plan_id = fields[0]
            template = int(fields[7]) if len(fields) > 7 and fields[7] else None
            os_label = fields[8] if len(fields) > 8 else None
            db.add_plan(
                plan_id, fields[1], int(fields[2]), int(fields[3]), int(fields[4]),
                int(fields[5]), int(fields[6]), template, os_label,
            )
        except ValueError:
            tg.send(chat_id, "Angka tidak valid. Cek cpu/ram/disk/harga/hari/template.")
            return True
        tg.send(chat_id, "Paket <b>" + tg.escape(fields[1]) + "</b> disimpan.")
        return True

    if command == "/delplan":
        if db.set_plan_enabled(rest, False):
            tg.send(chat_id, "Paket disembunyikan.")
        else:
            tg.send(chat_id, "Paket tidak ditemukan.")
        return True

    if command == "/allplans":
        rows = db.list_plans(only_enabled=False)
        if not rows:
            tg.send(chat_id, "Belum ada paket.")
            return True
        lines = []
        for p in rows:
            lines.append(
                ("[aktif] " if p["enabled"] else "[mati]  ")
                + p["id"] + " - " + p["name"] + " - " + provision.rupiah(p["price"])
                + " - template " + str(p["template_vmid"] or "-")
            )
        tg.send(chat_id, "<b>Semua Paket</b>\n\n" + tg.escape("\n".join(lines)))
        return True

    if command == "/extend":
        bits = rest.split()
        if len(bits) != 2:
            tg.send(chat_id, "Format: /extend <service_id> <hari>")
            return True
        try:
            service_id = int(bits[0])
            days = int(bits[1])
        except ValueError:
            tg.send(chat_id, "service_id dan hari harus angka.")
            return True
        service = db.get_service(service_id)
        if service is None:
            tg.send(chat_id, "VPS tidak ditemukan.")
            return True
        now = int(time.time())
        base = max(now, int(service["expires_at"]))
        db.set_expiry(service_id, base + days * 86400)
        if service["status"] in ("suspended", "error"):
            try:
                _provider.start(service)
                db.set_service_status(service_id, "active", now)
            except Exception as exc:
                tg.send(chat_id, "Masa aktif ditambah, tapi gagal nyalakan: " + str(exc))
                return True
        tg.send(chat_id, "VPS #" + str(service_id) + " ditambah " + str(days) + " hari.")
        tg.send(service["tg_id"], "Masa aktif VPS #" + str(service_id)
                + " ditambah " + str(days) + " hari oleh admin.")
        return True

    if command in ("/stopvps", "/startvps"):
        try:
            service_id = int(rest)
        except ValueError:
            tg.send(chat_id, "Format: " + command + " <service_id>")
            return True
        service = db.get_service(service_id)
        if service is None:
            tg.send(chat_id, "VPS tidak ditemukan.")
            return True
        try:
            if command == "/stopvps":
                _provider.stop(service)
                db.set_service_status(service_id, "suspended")
                tg.send(chat_id, "VPS #" + str(service_id) + " distop.")
            else:
                _provider.start(service)
                db.set_service_status(service_id, "active")
                tg.send(chat_id, "VPS #" + str(service_id) + " dinyalakan.")
        except Exception as exc:
            tg.send(chat_id, "Gagal: " + str(exc))
        return True

    if command == "/addip":
        ips = [p.strip() for p in rest.replace(";", ",").split(",") if p.strip()]
        added = db.seed_ip_pool(ips)
        used, total = db.ip_usage()
        tg.send(chat_id, str(added) + " IP ditambahkan. Pool: "
                + str(used) + "/" + str(total) + " terpakai.")
        return True

    if command == "/events":
        rows = db.recent_events(15)
        if not rows:
            tg.send(chat_id, "Belum ada kejadian.")
            return True
        lines = []
        for e in rows:
            stamp = time.strftime("%d/%m %H:%M", time.localtime(e["ts"]))
            lines.append(stamp + " " + e["kind"] + " " + str(e["detail"] or "")[:60])
        tg.send(chat_id, "<b>Kejadian Terakhir</b>\n\n" + tg.escape("\n".join(lines)))
        return True

    if command == "/admin":
        tg.send(chat_id, admin_help())
        return True

    return False


# ------------------------------------------------------------------ routing
def handle_message(message):
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    sender = message.get("from") or {}
    tg_id = sender.get("id")
    text = (message.get("text") or "").strip()

    if chat_id is None or tg_id is None or not text:
        return

    db.upsert_user(tg_id, sender.get("username"), sender.get("first_name"))

    if is_admin(tg_id) and text.startswith("/"):
        if handle_admin(chat_id, tg_id, text):
            return

    command = text.split()[0].lower()

    if command in ("/start", "/menu"):
        tg.send(chat_id, welcome_text(), main_menu())
        return

    if command == "/plans":
        body, keyboard = plans_view()
        tg.send(chat_id, body, keyboard)
        return

    if command == "/myvps":
        body, keyboard = services_view(tg_id)
        tg.send(chat_id, body, keyboard)
        return

    if command == "/orders":
        rows = db.list_user_orders(tg_id, 10)
        if not rows:
            tg.send(chat_id, "Belum ada pesanan.")
            return
        lines = []
        for o in rows:
            stamp = time.strftime("%d/%m %H:%M", time.localtime(o["created_at"]))
            lines.append(stamp + " " + o["id"] + " " + provision.rupiah(o["amount"])
                         + " - " + o["status"])
        tg.send(chat_id, "<b>Pesanan Terakhir</b>\n\n" + tg.escape("\n".join(lines)))
        return

    if command == "/help":
        tg.send(chat_id, help_text(), main_menu())
        return

    tg.send(chat_id, "Perintah tidak dikenal. Coba /help", main_menu())


def handle_callback(query):
    data = query.get("data") or ""
    callback_id = query.get("id")
    sender = query.get("from") or {}
    tg_id = sender.get("id")
    message = query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")

    if tg_id is None or chat_id is None:
        return

    db.upsert_user(tg_id, sender.get("username"), sender.get("first_name"))

    if data == "menu":
        tg.answer_callback(callback_id)
        tg.edit(chat_id, message_id, welcome_text(), main_menu())
        return

    if data == "plans":
        tg.answer_callback(callback_id)
        body, keyboard = plans_view()
        tg.edit(chat_id, message_id, body, keyboard)
        return

    if data == "myvps":
        tg.answer_callback(callback_id)
        body, keyboard = services_view(tg_id)
        tg.edit(chat_id, message_id, body, keyboard)
        return

    if data == "help":
        tg.answer_callback(callback_id)
        tg.edit(chat_id, message_id, help_text(), [back_button()])
        return

    if data.startswith("plan:"):
        tg.answer_callback(callback_id)
        body, keyboard = plan_detail_view(data[5:])
        tg.edit(chat_id, message_id, body, keyboard)
        return

    if data.startswith("buy:"):
        plan = db.get_plan(data[4:])
        if plan is None or not plan["enabled"]:
            tg.answer_callback(callback_id, "Paket tidak tersedia.", True)
            return
        if _pending_count(tg_id) >= MAX_PENDING_ORDERS:
            tg.answer_callback(
                callback_id,
                "Masih ada pesanan yang belum dibayar. Selesaikan dulu.",
                True,
            )
            return
        order = create_order_for(tg_id, plan, "new")
        tg.answer_callback(callback_id)
        body, keyboard = payment_view(order, plan, "new")
        tg.edit(chat_id, message_id, body, keyboard)
        return

    if data.startswith("renew:"):
        try:
            service_id = int(data[6:])
        except ValueError:
            tg.answer_callback(callback_id, "VPS tidak valid.", True)
            return
        service = db.get_service(service_id)
        if service is None or service["tg_id"] != tg_id:
            tg.answer_callback(callback_id, "VPS tidak ditemukan.", True)
            return
        if service["status"] == "terminated":
            tg.answer_callback(callback_id, "VPS sudah dihapus permanen.", True)
            return
        plan = db.get_plan(service["plan_id"])
        if plan is None:
            tg.answer_callback(callback_id, "Paket lama sudah tidak ada, hubungi admin.", True)
            return
        order = create_order_for(tg_id, plan, "renew", service_id)
        tg.answer_callback(callback_id)
        body, keyboard = payment_view(order, plan, "renew")
        tg.edit(chat_id, message_id, body, keyboard)
        return

    if data.startswith("svc:"):
        try:
            service_id = int(data[4:])
        except ValueError:
            tg.answer_callback(callback_id, "VPS tidak valid.", True)
            return
        tg.answer_callback(callback_id)
        body, keyboard = service_detail_view(tg_id, service_id)
        tg.edit(chat_id, message_id, body, keyboard)
        return

    if data.startswith("cred:"):
        try:
            service_id = int(data[5:])
        except ValueError:
            tg.answer_callback(callback_id, "VPS tidak valid.", True)
            return
        service = db.get_service(service_id)
        if service is None or service["tg_id"] != tg_id:
            tg.answer_callback(callback_id, "VPS tidak ditemukan.", True)
            return
        tg.answer_callback(callback_id)
        plan = db.get_plan(service["plan_id"])
        tg.send(chat_id, provision.credentials_text(service, plan))
        return

    if data.startswith("check:"):
        order_id = data[6:]
        result = check_payment_now(order_id, tg_id)
        tg.answer_callback(callback_id, result, True)
        return

    if data.startswith("cancel:"):
        order_id = data[7:]
        order = db.get_order(order_id)
        if order is None or order["tg_id"] != tg_id:
            tg.answer_callback(callback_id, "Order tidak ditemukan.", True)
            return
        if db.cancel_order(order_id):
            pakasir.cancel(order_id, order["amount"])
            tg.answer_callback(callback_id, "Order dibatalkan.")
            tg.edit(chat_id, message_id, "Order dibatalkan.", main_menu())
        else:
            tg.answer_callback(callback_id, "Order tidak bisa dibatalkan lagi.", True)
        return

    tg.answer_callback(callback_id)


def handle_update(update):
    try:
        if "message" in update:
            handle_message(update["message"])
        elif "callback_query" in update:
            handle_callback(update["callback_query"])
    except Exception as exc:
        # satu update bermasalah tidak boleh mematikan bot
        print("[handlers] error: " + str(exc))
