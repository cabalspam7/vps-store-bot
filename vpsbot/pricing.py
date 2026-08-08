"""Perhitungan harga: diskon reseller dan kupon.

Aturannya sengaja dibuat sederhana supaya tidak ada kejutan di struk:
- Diskon reseller menempel pada akun, otomatis, tanpa perlu kode.
- Kupon dipilih pelanggan lewat /kupon KODE.
- Kalau keduanya ada, yang dipakai hanya yang paling besar. Tidak ditumpuk,
  jadi harga akhir tidak pernah jatuh di bawah yang kita niatkan.
- Pembulatan ke bawah ke ratusan rupiah supaya nominal QRIS enak dibaca.
"""

import time

from . import db

MIN_AMOUNT = 1000  # batas bawah nominal QRIS


def _now():
    return int(time.time())


def coupon_problem(coupon, now=None):
    """None kalau kupon layak pakai, atau alasan penolakan dalam bahasa manusia."""
    now = now or _now()
    if coupon is None:
        return "Kode kupon tidak ditemukan."
    if not coupon["enabled"]:
        return "Kupon ini sudah tidak aktif."
    if coupon["expires_at"] and coupon["expires_at"] <= now:
        return "Kupon ini sudah kedaluwarsa."
    if coupon["max_uses"] and coupon["used"] >= coupon["max_uses"]:
        return "Kuota kupon ini sudah habis."
    return None


def usable_coupon(code, now=None):
    """Kembalikan (coupon_row, pesan_error). Salah satunya selalu None."""
    coupon = db.get_coupon(code)
    problem = coupon_problem(coupon, now=now)
    if problem:
        return None, problem
    return coupon, None


def _round_down(amount):
    amount = int(amount)
    amount = amount - (amount % 100)
    return max(MIN_AMOUNT, amount)


def quote(tg_id, base_amount, coupon_code=None, now=None, use_active=True):
    """Hitung harga akhir untuk satu pelanggan.

    Mengembalikan dict: base, amount, discount, percent, source, coupon_code.
    source: "none" | "reseller" | "coupon".
    Fungsi ini tidak menyentuh kuota kupon; pemesanan slot dilakukan
    db.claim_coupon() saat order benar-benar dibuat.
    """
    now = now or _now()
    base = int(base_amount)

    reseller_pct = db.get_reseller_pct(tg_id)

    if coupon_code is None and use_active:
        coupon_code = db.get_active_coupon(tg_id)

    coupon_pct = 0
    code = None
    if coupon_code:
        coupon, problem = usable_coupon(coupon_code, now=now)
        if coupon is not None and problem is None:
            coupon_pct = int(coupon["percent"])
            code = coupon["code"]

    if coupon_pct > reseller_pct:
        percent, source = coupon_pct, "coupon"
    elif reseller_pct > 0:
        percent, source, code = reseller_pct, "reseller", None
    else:
        percent, source, code = 0, "none", None

    if percent <= 0:
        return {
            "base": base,
            "amount": base,
            "discount": 0,
            "percent": 0,
            "source": "none",
            "coupon_code": None,
        }

    amount = _round_down(base - (base * percent) // 100)
    return {
        "base": base,
        "amount": amount,
        "discount": base - amount,
        "percent": percent,
        "source": source,
        "coupon_code": code,
    }


def discount_label(q):
    """Baris keterangan diskon untuk ditampilkan di struk."""
    if q["discount"] <= 0:
        return ""
    if q["source"] == "coupon":
        return "Kupon " + str(q["coupon_code"]) + " (-" + str(q["percent"]) + "%)"
    return "Diskon reseller (-" + str(q["percent"]) + "%)"
