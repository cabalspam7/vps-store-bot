"""Integrasi pembayaran QRIS Pakasir (produksi, bukan sandbox).

Alur yang dipakai: bot membuat link pembayaran hosted, lalu status transaksi
dicek berkala. Webhook opsional dipakai sebagai jalur cepat, tapi polling tetap
jalan supaya pembayaran tidak pernah tertinggal kalau webhook gagal masuk.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from . import config


class PakasirError(Exception):
    pass


def _post(path, payload, timeout=25):
    url = config.PAKASIR_BASE_URL + path
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise PakasirError("HTTP " + str(exc.code) + " pada " + path)
    except urllib.error.URLError as exc:
        raise PakasirError("jaringan: " + str(exc.reason))
    except json.JSONDecodeError:
        raise PakasirError("respons bukan JSON dari " + path)


def _get(path, params, timeout=25):
    url = config.PAKASIR_BASE_URL + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise PakasirError("HTTP " + str(exc.code) + " pada " + path)
    except urllib.error.URLError as exc:
        raise PakasirError("jaringan: " + str(exc.reason))
    except json.JSONDecodeError:
        raise PakasirError("respons bukan JSON dari " + path)


def checkout_url(order_id, amount):
    """Link pembayaran hosted, khusus QRIS, tanpa redirect otomatis."""
    query = urllib.parse.urlencode({"order_id": order_id, "qris_only": 1})
    return (
        config.PAKASIR_BASE_URL
        + "/pay/"
        + config.PAKASIR_PROJECT
        + "/"
        + str(int(amount))
        + "?"
        + query
    )


def create_qris(order_id, amount):
    """Opsional: bikin transaksi QRIS langsung (kalau mau tampilkan QR sendiri)."""
    return _post(
        "/api/transactioncreate/qris",
        {
            "project": config.PAKASIR_PROJECT,
            "order_id": order_id,
            "amount": int(amount),
            "api_key": config.PAKASIR_API_KEY,
        },
    )


def transaction_status(order_id, amount):
    """Kembalikan status transaksi: 'completed', 'pending', dll.

    Angka amount ikut dikirim karena Pakasir memakainya sebagai bagian
    identitas transaksi.
    """
    body = _get(
        "/api/transactiondetail",
        {
            "project": config.PAKASIR_PROJECT,
            "order_id": order_id,
            "amount": int(amount),
            "api_key": config.PAKASIR_API_KEY,
        },
    )
    trx = body.get("transaction") or {}
    return str(trx.get("status", "")).lower(), trx


def is_paid(order_id, amount):
    status, _ = transaction_status(order_id, amount)
    return status == "completed"


def cancel(order_id, amount):
    try:
        return _post(
            "/api/transactioncancel",
            {
                "project": config.PAKASIR_PROJECT,
                "order_id": order_id,
                "amount": int(amount),
                "api_key": config.PAKASIR_API_KEY,
            },
        )
    except PakasirError:
        return None


def verify_webhook(payload):
    """Validasi payload webhook. Jangan percaya isinya sebelum dicek ke API.

    Webhook hanya dipakai sebagai pemicu; kebenaran pembayaran tetap
    dipastikan lewat transaction_status().
    """
    if not isinstance(payload, dict):
        return None
    if str(payload.get("project", "")) != config.PAKASIR_PROJECT:
        return None
    order_id = payload.get("order_id")
    amount = payload.get("amount")
    if not order_id or amount is None:
        return None
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return None
    return {
        "order_id": str(order_id),
        "amount": amount,
        "status": str(payload.get("status", "")).lower(),
    }
