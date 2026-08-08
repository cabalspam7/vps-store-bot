"""Pekerja latar: cek pembayaran, kirim peringatan, stop VPS yang expired,
dan hapus VPS setelah masa tenggang.

Semua fungsi menerima parameter `now` supaya bisa diuji tanpa menunggu waktu
nyata. Ini yang bikin logika expiry bisa dites dalam hitungan milidetik.
"""

import time

from . import config, db, pakasir, provision, tg

# bitmask peringatan, satu bit per tahap (72 jam, 24 jam, 6 jam)
_WARN_FLAGS = {72: 1, 24: 2, 6: 4}


def _hours(seconds):
    return seconds / 3600.0


# ------------------------------------------------------------------ payments
def poll_payments(now=None, checker=None):
    """Cek order pending ke gateway. Ini jaring pengaman kalau webhook gagal."""
    now = now or int(time.time())
    checker = checker or pakasir.is_paid
    marked = 0

    for order in db.list_orders_by_status("pending"):
        try:
            paid = checker(order["id"], order["amount"])
        except Exception as exc:
            print("[sched] gagal cek pembayaran " + order["id"] + ": " + str(exc))
            continue
        if paid and db.mark_paid(order["id"], now):
            db.log_event("payment_confirmed", "poller", order["id"], None, now)
            marked += 1
    return marked


def expire_orders(now=None):
    now = now or int(time.time())
    count = db.expire_stale_orders(now)
    if count:
        db.log_event("orders_expired", str(count), None, None, now)
    return count


def fulfill_paid(provider, now=None):
    """Kerjakan semua order berstatus 'paid'.

    Sengaja terpisah dari deteksi pembayaran: kalau webhook menandai lunas lalu
    prosesnya mati, order tetap dikerjakan di tick berikutnya.
    """
    now = now or int(time.time())
    done = 0
    for order in db.list_orders_by_status("paid"):
        if provision.fulfill(order["id"], provider, now):
            done += 1
    return done


def requeue_stuck(now=None):
    """Order yang macet di 'provisioning' (misal bot mati di tengah proses)."""
    now = now or int(time.time())
    cutoff = now - config.STUCK_PROVISION_SECONDS
    count = 0
    for order in db.stuck_provisioning(cutoff):
        db.release_provision(order["id"], "macet, dicoba ulang")
        db.log_event("provision_requeued", "", order["id"], None, now)
        count += 1
    return count


# ------------------------------------------------------------------ reminders
def send_reminders(now=None):
    now = now or int(time.time())
    sent = 0

    for service in db.active_services(now):
        remaining = int(service["expires_at"]) - now
        if remaining <= 0:
            continue

        for hours in sorted(_WARN_FLAGS.keys()):
            flag = _WARN_FLAGS[hours]
            if _hours(remaining) > hours:
                continue
            # mark_warned bersyarat, jadi pesan tidak pernah dobel
            if not db.mark_warned(service["id"], flag):
                continue

            label = str(hours) + " jam"
            if hours >= 24:
                label = str(hours // 24) + " hari"

            tg.send(
                service["tg_id"],
                "<b>VPS akan expired</b>\n\nVPS #" + str(service["id"])
                + " (<code>" + str(service["ip"] or "-") + "</code>) habis dalam "
                + label + ".\nAktif sampai " + provision.fmt_expiry(service["expires_at"])
                + ".\n\nKalau tidak diperpanjang, VPS otomatis dimatikan saat expired. "
                + "Data masih disimpan " + str(config.GRACE_DAYS)
                + " hari sebelum dihapus permanen.\n\nPerpanjang: /myvps",
            )
            sent += 1
            break  # satu peringatan per tick
    return sent


# ------------------------------------------------------------------ suspend
def suspend_expired(provider, now=None):
    """Inti fitur: VPS yang lewat masa aktif langsung distop.

    Alur aman: claim_suspend() mengubah status jadi 'suspending' lebih dulu,
    jadi dua tick tidak akan menstop VPS yang sama bersamaan. Kalau stop gagal,
    status dikembalikan ke 'active' supaya dicoba lagi, bukan didiamkan.
    """
    now = now or int(time.time())
    stopped = 0

    for service in db.due_for_suspend(now):
        if not db.claim_suspend(service["id"]):
            continue

        try:
            provider.stop(service)
        except Exception as exc:
            # balikkan ke active supaya masuk antrean lagi di tick berikutnya
            db.set_service_status(service["id"], "active", now, str(exc)[:300])
            db.log_event("suspend_failed", str(exc), None, service["id"], now)
            tg.notify_admins(
                "Gagal stop VPS #" + str(service["id"]) + " (vmid "
                + str(service["vmid"]) + ")\nError: <code>"
                + tg.escape(str(exc)[:300]) + "</code>\nAkan dicoba ulang."
            )
            continue

        db.set_service_status(service["id"], "suspended", now)
        db.log_event("service_suspended", "expired", None, service["id"], now)
        stopped += 1

        tg.send(
            service["tg_id"],
            "<b>VPS dimatikan</b>\n\nVPS #" + str(service["id"]) + " (<code>"
            + str(service["ip"] or "-") + "</code>) sudah expired dan otomatis "
            "dimatikan.\n\nData masih utuh dan disimpan " + str(config.GRACE_DAYS)
            + " hari. Perpanjang lewat /myvps dan VPS langsung nyala lagi dengan "
            "data yang sama.\n\nLewat " + str(config.GRACE_DAYS)
            + " hari, VPS beserta datanya dihapus permanen.",
        )
    return stopped


# ------------------------------------------------------------------ terminate
def terminate_expired(provider, now=None):
    """Hapus VPS setelah masa tenggang habis. Bisa dimatikan via AUTO_DESTROY."""
    now = now or int(time.time())
    if not config.AUTO_DESTROY:
        return 0

    cutoff = now - config.GRACE_DAYS * 86400
    removed = 0

    for service in db.due_for_terminate(cutoff):
        if not db.claim_terminate(service["id"]):
            continue

        try:
            provider.destroy(service)
        except Exception as exc:
            db.set_service_status(service["id"], "suspended", now, str(exc)[:300])
            db.log_event("terminate_failed", str(exc), None, service["id"], now)
            tg.notify_admins(
                "Gagal hapus VPS #" + str(service["id"]) + ": <code>"
                + tg.escape(str(exc)[:300]) + "</code>"
            )
            continue

        db.free_ip(service["id"])
        db.set_service_status(service["id"], "terminated", now)
        db.log_event("service_terminated", "grace habis", None, service["id"], now)
        removed += 1

        tg.send(
            service["tg_id"],
            "<b>VPS dihapus</b>\n\nMasa tenggang VPS #" + str(service["id"])
            + " sudah habis, jadi VPS beserta datanya dihapus permanen.\n\n"
            "Mau mulai lagi? Lihat /plans",
        )
    return removed


# ------------------------------------------------------------------ main tick
def tick(provider, now=None, checker=None):
    """Satu siklus penuh. Urutannya penting."""
    now = now or int(time.time())
    result = {}
    steps = (
        ("expired_orders", lambda: expire_orders(now)),
        ("payments", lambda: poll_payments(now, checker)),
        ("requeued", lambda: requeue_stuck(now)),
        ("fulfilled", lambda: fulfill_paid(provider, now)),
        ("reminders", lambda: send_reminders(now)),
        ("suspended", lambda: suspend_expired(provider, now)),
        ("terminated", lambda: terminate_expired(provider, now)),
    )
    for name, fn in steps:
        try:
            result[name] = fn()
        except Exception as exc:
            # satu langkah gagal tidak boleh menghentikan langkah lainnya
            print("[sched] langkah " + name + " error: " + str(exc))
            result[name] = 0
    return result


def run_forever(provider, stop_event=None):
    print("[sched] jalan, interval " + str(config.CHECK_INTERVAL_SECONDS) + "s")
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            tick(provider)
        except Exception as exc:
            print("[sched] tick error: " + str(exc))
        if stop_event is not None:
            stop_event.wait(config.CHECK_INTERVAL_SECONDS)
        else:
            time.sleep(config.CHECK_INTERVAL_SECONDS)
