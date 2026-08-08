#!/usr/bin/env python3
"""Titik masuk bot. Jalankan: python3 bot.py"""

import signal
import sys
import threading

from vpsbot import config, db, handlers, providers, scheduler, tg, webhook

DEFAULT_PLANS = [
    # id, nama, cpu, ram_mb, disk_gb, harga, hari, template_vmid, os
    ("starter", "VPS Starter", 1, 1024, 20, 25000, 30, None, "Ubuntu 22.04"),
    ("basic", "VPS Basic", 2, 2048, 40, 45000, 30, None, "Ubuntu 22.04"),
    ("pro", "VPS Pro", 4, 4096, 80, 85000, 30, None, "Ubuntu 22.04"),
]

_stop = threading.Event()


def seed_defaults():
    if db.list_plans(only_enabled=False):
        return
    for row in DEFAULT_PLANS:
        db.add_plan(*row)
    print("[bot] paket contoh dibuat (ubah lewat /addplan)")


def preflight():
    missing = config.missing()
    if missing:
        print("Konfigurasi belum lengkap: " + ", ".join(missing))
        print("Salin .env.example lalu isi nilainya.")
        return False
    return True


def handle_signals():
    def _quit(signum, frame):
        print("\n[bot] berhenti...")
        _stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _quit)
        except Exception:
            pass


def main():
    if not preflight():
        return 1

    handle_signals()

    db.connect(config.DB_PATH)  # skema dibuat otomatis di sini
    seed_defaults()

    if config.IP_MODE == "pool" and config.IP_POOL:
        added = db.seed_ip_pool(config.IP_POOL)
        if added:
            print("[bot] " + str(added) + " IP ditambahkan ke pool")

    try:
        provider = providers.build()
    except Exception as exc:
        print("Gagal menyiapkan provider: " + str(exc))
        return 1
    print("[bot] provider: " + provider.name)

    handlers.setup(provider)

    me = tg.get_me()
    if not me:
        print("BOT_TOKEN ditolak Telegram. Periksa token dari @BotFather.")
        return 1
    print("[bot] login sebagai @" + str(me.get("username")))

    worker = threading.Thread(
        target=scheduler.run_forever, args=(provider, _stop), daemon=True
    )
    worker.start()

    if config.WEBHOOK_PORT:
        webhook.serve_in_thread(provider)
    else:
        print("[bot] webhook mati, pembayaran dicek lewat polling")

    # long polling butuh webhook Telegram dilepas dulu
    tg.delete_webhook()
    print("[bot] siap menerima pesan")

    offset = None
    while not _stop.is_set():
        try:
            updates = tg.get_updates(offset)
        except Exception as exc:
            print("[bot] getUpdates error: " + str(exc))
            _stop.wait(3)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            handlers.handle_update(update)

    print("[bot] selesai")
    return 0


if __name__ == "__main__":
    sys.exit(main())
