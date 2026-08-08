"""Penerima webhook Pakasir (opsional, jalur cepat).

Webhook hanya dipakai sebagai pemicu. Status lunas selalu diverifikasi ulang ke
API gateway, jadi request palsu tidak bisa membuat VPS gratis.

Poller di scheduler tetap jalan, jadi kalau webhook tidak sampai, order tetap
diproses. Webhook cuma mempercepat, bukan syarat.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, db, pakasir, provision

_provider = None


class _Handler(BaseHTTPRequestHandler):
    server_version = "vpsbot"

    def log_message(self, fmt, *args):
        return  # jangan banjiri log

    def _reply(self, code, body="ok"):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except Exception:
            pass

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/health"):
            self._reply(200, "ok")
        else:
            self._reply(404, "not found")

    def do_POST(self):
        if self.path.rstrip("/") not in ("/webhook", "/pakasir", ""):
            self._reply(404, "not found")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 64 * 1024:
            self._reply(400, "bad length")
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._reply(400, "bad json")
            return

        # selalu balas 200 supaya gateway tidak spam retry;
        # pemrosesan dilakukan di thread lain
        self._reply(200, "ok")

        order_id = payload.get("order_id")
        if not order_id:
            return

        thread = threading.Thread(
            target=_process, args=(order_id, payload), daemon=True
        )
        thread.start()


def _process(order_id, payload):
    try:
        order = db.get_order(order_id)
        if order is None:
            print("[webhook] order tidak dikenal: " + str(order_id))
            return

        # jangan percaya isi webhook; tanya balik ke gateway
        if not pakasir.verify_webhook(payload):
            print("[webhook] verifikasi gagal untuk " + str(order_id))
            return

        if db.mark_paid(order_id):
            db.log_event("payment_confirmed", "webhook", order_id)

        provision.fulfill(order_id, _provider)
    except Exception as exc:
        print("[webhook] error proses " + str(order_id) + ": " + str(exc))


def serve(provider, port=None):
    global _provider
    _provider = provider
    port = port or config.WEBHOOK_PORT
    httpd = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    print("[webhook] listen di port " + str(port))
    httpd.serve_forever()


def serve_in_thread(provider, port=None):
    thread = threading.Thread(target=serve, args=(provider, port), daemon=True)
    thread.start()
    return thread
