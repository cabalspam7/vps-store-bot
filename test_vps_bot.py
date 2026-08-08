#!/usr/bin/env python3
"""Tes siklus hidup penuh bot VPS pakai driver mock.

Waktu disuntik lewat parameter `now`, jadi skenario 30 hari bisa diuji dalam
hitungan milidetik tanpa menunggu.
"""

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TMP = tempfile.mkdtemp(prefix="vpsbot-test-")
os.environ.update({
    "BOT_TOKEN": "test:token",
    "ADMIN_IDS": "1",
    "PAKASIR_PROJECT": "demo",
    "PAKASIR_API_KEY": "secret",
    "PROVIDER": "mock",
    "IP_MODE": "pool",
    "IP_POOL": "10.10.0.10,10.10.0.11,10.10.0.12",
    "PROXMOX_GATEWAY": "10.10.0.1",
    "GRACE_DAYS": "3",
    "DB_PATH": os.path.join(TMP, "test.db"),
})

from vpsbot import config, db, handlers, pakasir, pricing, provision, scheduler, tg  # noqa: E402
from vpsbot.providers.mock import MockProvisioner  # noqa: E402

USER = 555001
USER2 = 555002
DAY = 86400

SENT = []
PAID = {"value": False}


def fake_send(chat_id, text, keyboard=None, preview=False):
    SENT.append((chat_id, text))
    return {"message_id": len(SENT)}


tg.send = fake_send
tg.edit = lambda *a, **k: None
tg.answer_callback = lambda *a, **k: None
tg.notify_admins = lambda text: SENT.append((0, text))
pakasir.is_paid = lambda order_id, amount: PAID["value"]
pakasir.cancel = lambda order_id, amount: True


def last_text():
    return SENT[-1][1] if SENT else ""


def texts_since(mark):
    return [t for _, t in SENT[mark:]]


def check(label, condition, extra=""):
    if condition:
        print("  ok   " + label)
    else:
        print("  FAIL " + label + ((" -> " + str(extra)) if extra else ""))
        raise AssertionError(label)


def buy_callback(plan_id, user=USER):
    handlers.handle_callback({
        "id": "cb",
        "from": {"id": user, "username": "tester"},
        "data": "buy:" + plan_id,
        "message": {"message_id": 10, "chat": {"id": user}},
    })


def latest_order(user=USER):
    rows = db.list_user_orders(user, 1)
    return rows[0] if rows else None


def buy_new_order(user, plan_id="starter"):
    """Beli lalu ambil order yang benar-benar baru.

    Beberapa order bisa lahir di detik yang sama, jadi urutan created_at tidak
    bisa dipakai untuk menebak yang terbaru.
    """
    before = set(o["id"] for o in db.list_user_orders(user, 50))
    buy_callback(plan_id, user)
    fresh = [o for o in db.list_user_orders(user, 50) if o["id"] not in before]
    check("order baru dibuat untuk " + str(user), len(fresh) == 1, len(fresh))
    return fresh[0]


# --------------------------------------------------------------------- setup
def setup():
    print("[setup] database & provider")
    db.connect(config.DB_PATH)
    db.seed_ip_pool(config.IP_POOL)
    db.add_plan("starter", "VPS Starter", 1, 1024, 20, 25000, 30, 9001, "Ubuntu 22.04")
    provider = MockProvisioner(state_path=os.path.join(TMP, "mock.json"))
    handlers.setup(provider)
    check("paket tersimpan", db.get_plan("starter") is not None)
    used, total = db.ip_usage()
    check("pool IP terisi", total == 3 and used == 0, (used, total))
    return provider


# --------------------------------------------------------------------- tests
def test_menu_and_order(provider, t0):
    print("[1] menu, pesan, dan tagihan")
    handlers.handle_message({
        "chat": {"id": USER},
        "from": {"id": USER, "username": "tester", "first_name": "Tester"},
        "text": "/start",
    })
    check("balasan /start terkirim", config.STORE_NAME in last_text())

    buy_callback("starter")
    order = latest_order()
    check("order dibuat", order is not None and order["status"] == "pending", order)
    check("jumlah sesuai harga paket", order["amount"] == 25000, order["amount"])
    check("jenis order = new", order["kind"] == "new")

    url = pakasir.checkout_url(order["id"], order["amount"])
    check("link bayar mengandung order_id", order["id"] in url, url)

    # belum dibayar -> tidak boleh ada VPS
    result = scheduler.tick(provider, now=t0, checker=lambda o, a: False)
    check("tanpa bayar tidak ada VPS", result["fulfilled"] == 0, result)
    check("order masih pending", db.get_order(order["id"])["status"] == "pending")
    return order["id"]


def test_payment_and_provision(provider, order_id, t0):
    print("[2] pembayaran lunas -> VPS otomatis dibuat")
    mark = len(SENT)
    PAID["value"] = True
    result = scheduler.tick(provider, now=t0)
    check("1 order dipenuhi", result["fulfilled"] == 1, result)

    order = db.get_order(order_id)
    check("order selesai", order["status"] == "done", order["status"])

    service = db.get_service(order["service_id"])
    check("layanan aktif", service["status"] == "active", service["status"])
    check("IP dari pool", service["ip"] in config.IP_POOL, service["ip"])
    check("masa aktif 30 hari", service["expires_at"] == t0 + 30 * DAY,
          service["expires_at"] - t0)
    check("VM menyala di provider", provider.status(service) == "running")
    check("password digenerate", bool(service["password"]))

    body = "\n".join(texts_since(mark))
    check("data login dikirim ke pelanggan", service["ip"] in body and service["password"] in body)

    used, _ = db.ip_usage()
    check("1 IP terpakai", used == 1, used)
    return service["id"]


def test_exactly_once(provider, order_id, service_id, t0):
    print("[3] anti dobel: webhook + poller bersamaan")
    check("mark_paid kedua ditolak", db.mark_paid(order_id, t0) is False)
    check("fulfill kedua tidak jalan", provision.fulfill(order_id, provider, t0) is False)
    check("tetap 1 VPS", len(db.list_services(USER)) == 1)
    check("tetap 1 IP terpakai", db.ip_usage()[0] == 1)

    # jalankan tick lagi: tidak boleh bikin VPS kedua
    result = scheduler.tick(provider, now=t0)
    check("tick ulang tidak menambah VPS", result["fulfilled"] == 0 and len(db.list_services(USER)) == 1)


def test_reminders(provider, service_id):
    print("[4] peringatan 3 hari / 1 hari / 6 jam, masing-masing sekali")
    expires = db.get_service(service_id)["expires_at"]

    for label, offset in (("72 jam", 70 * 3600), ("24 jam", 20 * 3600), ("6 jam", 5 * 3600)):
        now = expires - offset
        sent = scheduler.send_reminders(now)
        check("peringatan " + label + " terkirim", sent == 1, sent)
        again = scheduler.send_reminders(now)
        check("peringatan " + label + " tidak dobel", again == 0, again)

    check("belum disuspend sebelum expired",
          db.get_service(service_id)["status"] == "active")


def test_suspend_on_expiry(provider, service_id):
    print("[5] expired -> VPS otomatis distop")
    expires = db.get_service(service_id)["expires_at"]
    now = expires + 60

    # kegagalan stop harus dicoba ulang, bukan didiamkan
    provider.fail_stop = True
    stopped = scheduler.suspend_expired(provider, now)
    check("stop gagal tidak menandai suspended", stopped == 0, stopped)
    check("status kembali active untuk dicoba lagi",
          db.get_service(service_id)["status"] == "active",
          db.get_service(service_id)["status"])
    check("VM masih menyala", provider.status(db.get_service(service_id)) == "running")

    provider.fail_stop = False
    mark = len(SENT)
    stopped = scheduler.suspend_expired(provider, now)
    check("percobaan berikutnya berhasil", stopped == 1, stopped)

    service = db.get_service(service_id)
    check("status suspended", service["status"] == "suspended", service["status"])
    check("VM benar-benar mati", provider.status(service) == "stopped")
    check("suspended_at tercatat", service["suspended_at"] == now)
    check("IP belum dilepas (data masih ada)", db.ip_usage()[0] == 1)
    check("pelanggan diberi tahu", "dimatikan" in "\n".join(texts_since(mark)).lower())

    # tick kedua tidak boleh menstop ulang
    check("tidak distop dua kali", scheduler.suspend_expired(provider, now + 10) == 0)
    return now


def test_renew_restores(provider, service_id, suspended_at):
    print("[6] perpanjangan -> VPS nyala lagi dengan data yang sama")
    now = suspended_at + 3600
    plan = db.get_plan("starter")
    order = handlers.create_order_for(USER, plan, "renew", service_id, now=now)
    check("order perpanjangan dibuat", order["kind"] == "renew")

    old_vmid = db.get_service(service_id)["vmid"]
    old_ip = db.get_service(service_id)["ip"]

    check("pembayaran tercatat", db.mark_paid(order["id"], now) is True)
    check("perpanjangan diproses", provision.fulfill(order["id"], provider, now) is True)

    service = db.get_service(service_id)
    check("aktif kembali", service["status"] == "active", service["status"])
    check("VM menyala lagi", provider.status(service) == "running")
    check("VM sama, data tidak hilang", service["vmid"] == old_vmid and service["ip"] == old_ip)
    check("masa aktif +30 hari dari sekarang",
          service["expires_at"] == now + 30 * DAY, service["expires_at"] - now)
    check("flag peringatan direset", service["warn_flags"] == 0, service["warn_flags"])
    return now


def test_renew_before_expiry_keeps_remaining(provider, service_id, now):
    print("[7] perpanjang sebelum expired -> sisa waktu tidak hangus")
    before = db.get_service(service_id)["expires_at"]
    early = now + 5 * DAY  # masih aktif
    plan = db.get_plan("starter")
    order = handlers.create_order_for(USER, plan, "renew", service_id, now=early)
    db.mark_paid(order["id"], early)
    provision.fulfill(order["id"], provider, early)

    after = db.get_service(service_id)["expires_at"]
    check("ditambahkan ke sisa waktu, bukan dari hari ini",
          after == before + 30 * DAY, (after - before) // DAY)
    return early


def test_grace_and_terminate(provider, service_id):
    print("[8] masa tenggang lalu hapus permanen")
    expires = db.get_service(service_id)["expires_at"]
    suspend_at = expires + 60
    scheduler.suspend_expired(provider, suspend_at)
    check("disuspend lagi", db.get_service(service_id)["status"] == "suspended")

    mid_grace = suspend_at + 1 * DAY
    check("belum dihapus di tengah masa tenggang",
          scheduler.terminate_expired(provider, mid_grace) == 0)
    check("VM masih ada", provider.status(db.get_service(service_id)) == "stopped")

    after_grace = suspend_at + config.GRACE_DAYS * DAY + 60

    config.AUTO_DESTROY = False
    check("AUTO_DESTROY=False menahan penghapusan",
          scheduler.terminate_expired(provider, after_grace) == 0)
    config.AUTO_DESTROY = True

    mark = len(SENT)
    removed = scheduler.terminate_expired(provider, after_grace)
    check("dihapus setelah masa tenggang", removed == 1, removed)

    service = db.get_service(service_id)
    check("status terminated", service["status"] == "terminated", service["status"])
    check("VM hilang dari provider", provider.status(service) == "missing")
    check("IP kembali ke pool", db.ip_usage()[0] == 0, db.ip_usage())
    check("pelanggan diberi tahu", "dihapus" in "\n".join(texts_since(mark)).lower())
    check("tidak muncul lagi di /myvps", len(db.list_services(USER)) == 0)


def test_failed_provision(t0):
    print("[9] provisioning gagal -> dicoba ulang, lalu ditandai gagal")
    broken = MockProvisioner(state_path=os.path.join(TMP, "broken.json"), fail_create=True)
    plan = db.get_plan("starter")
    order = handlers.create_order_for(USER2, plan, "new", None, now=t0)
    db.mark_paid(order["id"], t0)

    provision.fulfill(order["id"], broken, t0)
    check("percobaan 1 gagal -> kembali ke paid",
          db.get_order(order["id"])["status"] == "paid",
          db.get_order(order["id"])["status"])
    service_id = db.get_order(order["id"])["service_id"]

    provision.fulfill(order["id"], broken, t0)
    check("percobaan 2 masih dicoba ulang",
          db.get_order(order["id"])["status"] == "paid")
    check("tidak bikin layanan kedua",
          db.get_order(order["id"])["service_id"] == service_id)

    mark = len(SENT)
    provision.fulfill(order["id"], broken, t0)
    final = db.get_order(order["id"])
    check("percobaan ke-3 ditandai gagal", final["status"] == "failed", final["status"])
    check("attempts tercatat", final["attempts"] == config.MAX_PROVISION_ATTEMPTS,
          final["attempts"])
    check("IP dilepas kembali", db.ip_usage()[0] == 0, db.ip_usage())
    body = "\n".join(texts_since(mark))
    check("admin diberi tahu", "PERLU TINDAKAN" in body)
    check("pelanggan diberi tahu uangnya tidak hilang", "tidak hilang" in body)


def test_stuck_requeue(provider, t0):
    print("[10] proses macet -> dikembalikan ke antrean")
    plan = db.get_plan("starter")
    order = handlers.create_order_for(USER2, plan, "new", None, now=t0)
    db.mark_paid(order["id"], t0)
    db.claim_provision(order["id"], t0)
    check("status provisioning", db.get_order(order["id"])["status"] == "provisioning")

    check("belum dianggap macet", scheduler.requeue_stuck(t0 + 10) == 0)
    requeued = scheduler.requeue_stuck(t0 + config.STUCK_PROVISION_SECONDS + 10)
    check("dikembalikan ke paid", requeued == 1 and db.get_order(order["id"])["status"] == "paid")

    # sekarang bisa diselesaikan provider yang sehat
    done = scheduler.fulfill_paid(provider, t0)
    check("order lanjut selesai", done == 1 and db.get_order(order["id"])["status"] == "done")


def test_order_expiry(t0):
    print("[11] order tidak dibayar -> kedaluwarsa")
    plan = db.get_plan("starter")
    order = handlers.create_order_for(USER2, plan, "new", None, now=t0)
    later = t0 + config.ORDER_EXPIRE_MINUTES * 60 + 60
    check("ditandai expired", scheduler.expire_orders(later) >= 1)
    check("status expired", db.get_order(order["id"])["status"] == "expired")


def test_cancel_order(t0):
    print("[12] pelanggan membatalkan order")
    plan = db.get_plan("starter")
    order = handlers.create_order_for(USER2, plan, "new", None, now=t0)
    handlers.handle_callback({
        "id": "cb",
        "from": {"id": USER2},
        "data": "cancel:" + order["id"],
        "message": {"message_id": 11, "chat": {"id": USER2}},
    })
    check("status canceled", db.get_order(order["id"])["status"] == "canceled")
    check("order milik orang lain tidak bisa dibatalkan",
          db.cancel_order(order["id"]) is False)


def test_isolation(provider):
    print("[13] pelanggan tidak bisa lihat VPS orang lain")
    services = db.list_services(USER2)
    check("USER2 punya VPS", len(services) >= 1, len(services))
    target = services[0]["id"]
    body, _ = handlers.service_detail_view(USER, target)
    check("akses lintas akun ditolak", "tidak ditemukan" in body.lower(), body)


def test_admin_tools(provider, t0):
    print("[14] perintah admin")
    admin_id = 1
    mark = len(SENT)
    handlers.handle_message({
        "chat": {"id": admin_id},
        "from": {"id": admin_id},
        "text": "/stats",
    })
    check("statistik tampil", "Statistik" in "\n".join(texts_since(mark)))

    service = db.list_services(USER2)[0]
    before = service["expires_at"]
    handlers.handle_message({
        "chat": {"id": admin_id},
        "from": {"id": admin_id},
        "text": "/extend " + str(service["id"]) + " 7",
    })
    after = db.get_service(service["id"])["expires_at"]
    check("masa aktif ditambah 7 hari", after == before + 7 * DAY, (after - before) // DAY)

    handlers.handle_message({
        "chat": {"id": admin_id},
        "from": {"id": admin_id},
        "text": "/stopvps " + str(service["id"]),
    })
    check("admin bisa stop manual",
          provider.status(db.get_service(service["id"])) == "stopped")

    handlers.handle_message({
        "chat": {"id": admin_id},
        "from": {"id": admin_id},
        "text": "/startvps " + str(service["id"]),
    })
    check("admin bisa nyalakan manual",
          provider.status(db.get_service(service["id"])) == "running")

    handlers.handle_message({
        "chat": {"id": USER},
        "from": {"id": USER},
        "text": "/stats",
    })
    check("non-admin tidak dapat statistik", "Statistik" not in last_text())


def test_coupon_discount(t0):
    print("[15] kupon diskon")
    user = 555003
    db.add_coupon("HEMAT20", 20, 0, None, t0)

    handlers.handle_message({
        "chat": {"id": user},
        "from": {"id": user, "username": "kuponer"},
        "text": "/kupon hemat20",
    })
    check("kupon diterima & dinormalkan huruf besar", "HEMAT20" in last_text())
    check("kupon tersimpan di akun", db.get_active_coupon(user) == "HEMAT20")

    body, _ = handlers.plan_detail_view("starter", user)
    check("harga coret muncul di detail paket", "Bayar" in body and "20.000" in body, body)

    order = buy_new_order(user)
    check("total dipotong 20%", order["amount"] == 20000, order["amount"])
    check("harga normal disimpan", order["base_amount"] == 25000, order["base_amount"])
    check("nilai diskon dicatat", order["discount"] == 5000, order["discount"])
    check("kode kupon menempel di order", order["coupon_code"] == "HEMAT20")

    coupon = db.get_coupon("HEMAT20")
    check("pemakaian kupon dihitung", coupon["used"] == 1, coupon["used"])
    check("kupon dilepas setelah dipakai", db.get_active_coupon(user) is None)

    kedua = buy_new_order(user)
    check("order berikutnya harga normal", kedua["amount"] == 25000, kedua["amount"])
    check("kupon tidak terhitung dua kali", db.get_coupon("HEMAT20")["used"] == 1)


def test_reseller_discount(t0):
    print("[16] diskon reseller")
    admin_id = sorted(config.ADMIN_IDS)[0]
    user = 555004

    handlers.handle_message({
        "chat": {"id": admin_id},
        "from": {"id": admin_id},
        "text": "/reseller " + str(user) + " 30",
    })
    check("reseller tersimpan", db.get_reseller_pct(user) == 30, db.get_reseller_pct(user))

    order = buy_new_order(user)
    check("total dipotong 30%", order["amount"] == 17500, order["amount"])
    check("diskon reseller dicatat", order["discount"] == 7500, order["discount"])
    check("tanpa kode kupon", order["coupon_code"] is None)

    body, _ = handlers.plans_view(user)
    check("info diskon tampil di daftar paket", "reseller" in body.lower(), body)

    # kupon lebih kecil tidak boleh mengalahkan diskon reseller
    db.add_coupon("KECIL10", 10, 0, None, t0)
    handlers.handle_message({
        "chat": {"id": user},
        "from": {"id": user},
        "text": "/kupon KECIL10",
    })
    q = pricing.quote(user, 25000)
    check("yang dipakai diskon terbesar", q["source"] == "reseller" and q["amount"] == 17500, q)
    check("diskon tidak ditumpuk", q["discount"] == 7500, q)

    handlers.handle_message({
        "chat": {"id": admin_id},
        "from": {"id": admin_id},
        "text": "/reseller " + str(user) + " 0",
    })
    check("status reseller bisa dicabut", db.get_reseller_pct(user) == 0)


def test_coupon_rules(t0):
    print("[17] batas kuota, kedaluwarsa, dan pengembalian kupon")
    admin_id = sorted(config.ADMIN_IDS)[0]
    user = 555005

    handlers.handle_message({
        "chat": {"id": admin_id},
        "from": {"id": admin_id},
        "text": "/addkupon SEKALI|15|1|30",
    })
    coupon = db.get_coupon("SEKALI")
    check("kupon admin dibuat", coupon is not None and coupon["percent"] == 15, coupon)
    check("kuota 1 tersimpan", coupon["max_uses"] == 1)
    check("masa berlaku tersimpan", bool(coupon["expires_at"]))

    handlers.handle_message({
        "chat": {"id": user}, "from": {"id": user}, "text": "/kupon SEKALI",
    })
    order = buy_new_order(user)
    # 15% dari 25.000 = 21.250, dibulatkan turun ke ratusan jadi 21.200
    check("kupon terpakai", order["amount"] == 21200, order["amount"])
    check("nominal dibulatkan turun ke ratusan", order["discount"] == 3800,
          order["discount"])
    check("kuota terpakai penuh", db.get_coupon("SEKALI")["used"] == 1)

    # kuota habis -> pelanggan lain ditolak saat memasang kupon
    user2 = 555006
    handlers.handle_message({
        "chat": {"id": user2}, "from": {"id": user2}, "text": "/kupon SEKALI",
    })
    check("kuota habis ditolak", "habis" in last_text().lower(), last_text())
    check("kupon tidak menempel", db.get_active_coupon(user2) is None)

    # order dibatalkan -> slot kupon kembali
    handlers.handle_callback({
        "id": "cb",
        "from": {"id": user},
        "data": "cancel:" + order["id"],
        "message": {"message_id": 11, "chat": {"id": user}},
    })
    check("order batal", db.get_order(order["id"])["status"] == "canceled")
    check("slot kupon dikembalikan", db.get_coupon("SEKALI")["used"] == 0,
          db.get_coupon("SEKALI")["used"])

    # order hangus -> slot kupon juga kembali
    handlers.handle_message({
        "chat": {"id": user}, "from": {"id": user}, "text": "/kupon SEKALI",
    })
    hangus = buy_new_order(user)
    check("kupon dipakai lagi", db.get_coupon("SEKALI")["used"] == 1)
    db.expire_stale_orders(now=hangus["expires_at"] + 1)
    check("order hangus", db.get_order(hangus["id"])["status"] == "expired")
    check("slot kupon kembali saat hangus", db.get_coupon("SEKALI")["used"] == 0)

    # kupon kedaluwarsa
    db.add_coupon("KADALUARSA", 50, 0, t0 - DAY, t0)
    handlers.handle_message({
        "chat": {"id": user}, "from": {"id": user}, "text": "/kupon KADALUARSA",
    })
    check("kupon kedaluwarsa ditolak", "kedaluwarsa" in last_text().lower(), last_text())

    # kupon dimatikan admin
    handlers.handle_message({
        "chat": {"id": admin_id}, "from": {"id": admin_id}, "text": "/delkupon SEKALI",
    })
    handlers.handle_message({
        "chat": {"id": user}, "from": {"id": user}, "text": "/kupon SEKALI",
    })
    check("kupon nonaktif ditolak", "aktif" in last_text().lower(), last_text())

    # kode ngawur
    handlers.handle_message({
        "chat": {"id": user}, "from": {"id": user}, "text": "/kupon NGAWUR123",
    })
    check("kode asal ditolak", "tidak ditemukan" in last_text().lower(), last_text())

    # lepas kupon
    db.set_active_coupon(user, "HEMAT20")
    handlers.handle_message({
        "chat": {"id": user}, "from": {"id": user}, "text": "/kupon hapus",
    })
    check("kupon bisa dilepas", db.get_active_coupon(user) is None)

    # daftar kupon untuk admin
    handlers.handle_message({
        "chat": {"id": admin_id}, "from": {"id": admin_id}, "text": "/kupons",
    })
    check("daftar kupon tampil", "SEKALI" in last_text(), last_text())

    # pelanggan biasa tidak bisa bikin kupon
    handlers.handle_message({
        "chat": {"id": user}, "from": {"id": user}, "text": "/addkupon BOCOR|90|0|0",
    })
    check("non-admin tidak bisa bikin kupon", db.get_coupon("BOCOR") is None)


def main():
    t0 = int(time.time())
    provider = setup()
    try:
        order_id = test_menu_and_order(provider, t0)
        service_id = test_payment_and_provision(provider, order_id, t0)
        test_exactly_once(provider, order_id, service_id, t0)
        test_reminders(provider, service_id)
        suspended_at = test_suspend_on_expiry(provider, service_id)
        renewed_at = test_renew_restores(provider, service_id, suspended_at)
        test_renew_before_expiry_keeps_remaining(provider, service_id, renewed_at)
        test_grace_and_terminate(provider, service_id)
        test_failed_provision(t0)
        test_stuck_requeue(provider, t0)
        test_order_expiry(t0)
        test_cancel_order(t0)
        test_isolation(provider)
        test_admin_tools(provider, t0)
        test_coupon_discount(t0)
        test_reseller_discount(t0)
        test_coupon_rules(t0)
    finally:
        db.close()
        shutil.rmtree(TMP, ignore_errors=True)

    print("\nALL_TESTS_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
