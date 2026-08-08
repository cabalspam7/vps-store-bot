"""Lapisan SQLite. Semua penulisan dikunci supaya aman dipakai beberapa thread.

Catatan penting soal uang & provisioning:
- mark_paid() dan claim_provision() memakai UPDATE bersyarat.
  Yang menang rowcount==1, jadi order tidak pernah diproses dua kali
  walau webhook dan poller datang bersamaan.
"""

import os
import sqlite3
import threading
import time

_lock = threading.RLock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id      INTEGER PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    cpu           INTEGER NOT NULL,
    ram_mb        INTEGER NOT NULL,
    disk_gb       INTEGER NOT NULL,
    price         INTEGER NOT NULL,
    period_days   INTEGER NOT NULL,
    template_vmid INTEGER,
    os_label      TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    sort_order    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS orders (
    id             TEXT PRIMARY KEY,
    tg_id          INTEGER NOT NULL,
    plan_id        TEXT NOT NULL,
    kind           TEXT NOT NULL,
    service_id     INTEGER,
    amount         INTEGER NOT NULL,
    status         TEXT NOT NULL,
    attempts       INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL,
    expires_at     INTEGER NOT NULL,
    paid_at        INTEGER,
    claimed_at     INTEGER,
    finished_at    INTEGER,
    last_error     TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(tg_id);

CREATE TABLE IF NOT EXISTS services (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id         INTEGER NOT NULL,
    plan_id       TEXT NOT NULL,
    provider      TEXT NOT NULL,
    node          TEXT,
    vmid          INTEGER,
    hostname      TEXT,
    ip            TEXT,
    ssh_port      INTEGER,
    username      TEXT,
    password      TEXT,
    status        TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL,
    warn_flags    INTEGER NOT NULL DEFAULT 0,
    suspended_at  INTEGER,
    terminated_at INTEGER,
    last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_services_user ON services(tg_id);
CREATE INDEX IF NOT EXISTS idx_services_status ON services(status);

CREATE TABLE IF NOT EXISTS ip_pool (
    ip         TEXT PRIMARY KEY,
    service_id INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    order_id   TEXT,
    service_id INTEGER,
    detail     TEXT
);
"""


def connect(path):
    global _conn
    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=FULL")
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def close():
    global _conn
    with _lock:
        if _conn is not None:
            _conn.commit()
            _conn.close()
            _conn = None


def _now():
    return int(time.time())


def _exec(sql, params=()):
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur


def _one(sql, params=()):
    with _lock:
        cur = _conn.execute(sql, params)
        return cur.fetchone()


def _all(sql, params=()):
    with _lock:
        cur = _conn.execute(sql, params)
        return cur.fetchall()


# ------------------------------------------------------------------ events
def log_event(kind, detail="", order_id=None, service_id=None, now=None):
    _exec(
        "INSERT INTO events (ts, kind, order_id, service_id, detail) VALUES (?,?,?,?,?)",
        (now or _now(), kind, order_id, service_id, str(detail)[:2000]),
    )


def recent_events(limit=20):
    return _all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))


# ------------------------------------------------------------------ users
def upsert_user(tg_id, username=None, first_name=None, now=None):
    row = _one("SELECT tg_id FROM users WHERE tg_id=?", (tg_id,))
    if row is None:
        _exec(
            "INSERT INTO users (tg_id, username, first_name, created_at) VALUES (?,?,?,?)",
            (tg_id, username, first_name, now or _now()),
        )
    else:
        _exec(
            "UPDATE users SET username=?, first_name=? WHERE tg_id=?",
            (username, first_name, tg_id),
        )


def count_users():
    row = _one("SELECT COUNT(*) AS n FROM users")
    return row["n"] if row else 0


# ------------------------------------------------------------------ plans
def add_plan(plan_id, name, cpu, ram_mb, disk_gb, price, period_days,
             template_vmid=None, os_label=None, sort_order=0):
    _exec(
        """INSERT INTO plans (id, name, cpu, ram_mb, disk_gb, price, period_days,
                              template_vmid, os_label, enabled, sort_order)
           VALUES (?,?,?,?,?,?,?,?,?,1,?)
           ON CONFLICT(id) DO UPDATE SET
             name=excluded.name, cpu=excluded.cpu, ram_mb=excluded.ram_mb,
             disk_gb=excluded.disk_gb, price=excluded.price,
             period_days=excluded.period_days, template_vmid=excluded.template_vmid,
             os_label=excluded.os_label, sort_order=excluded.sort_order""",
        (plan_id, name, cpu, ram_mb, disk_gb, price, period_days,
         template_vmid, os_label, sort_order),
    )


def set_plan_enabled(plan_id, enabled):
    cur = _exec("UPDATE plans SET enabled=? WHERE id=?", (1 if enabled else 0, plan_id))
    return cur.rowcount == 1


def get_plan(plan_id):
    return _one("SELECT * FROM plans WHERE id=?", (plan_id,))


def list_plans(only_enabled=True):
    if only_enabled:
        return _all("SELECT * FROM plans WHERE enabled=1 ORDER BY sort_order, price")
    return _all("SELECT * FROM plans ORDER BY sort_order, price")


# ------------------------------------------------------------------ orders
def create_order(order_id, tg_id, plan_id, kind, amount, expires_at,
                 service_id=None, now=None):
    _exec(
        """INSERT INTO orders (id, tg_id, plan_id, kind, service_id, amount,
                               status, created_at, expires_at)
           VALUES (?,?,?,?,?,?,'pending',?,?)""",
        (order_id, tg_id, plan_id, kind, service_id, amount, now or _now(), expires_at),
    )
    return get_order(order_id)


def get_order(order_id):
    return _one("SELECT * FROM orders WHERE id=?", (order_id,))


def list_orders_by_status(status):
    return _all("SELECT * FROM orders WHERE status=? ORDER BY created_at", (status,))


def list_user_orders(tg_id, limit=10):
    return _all(
        "SELECT * FROM orders WHERE tg_id=? ORDER BY created_at DESC LIMIT ?",
        (tg_id, limit),
    )


def mark_paid(order_id, now=None):
    """True hanya untuk pemanggil pertama. Mencegah pembayaran dihitung dobel."""
    cur = _exec(
        "UPDATE orders SET status='paid', paid_at=? WHERE id=? AND status='pending'",
        (now or _now(), order_id),
    )
    return cur.rowcount == 1


def claim_provision(order_id, now=None):
    """True hanya untuk satu worker. Kunci supaya VPS tidak dibuat dua kali."""
    cur = _exec(
        """UPDATE orders SET status='provisioning', claimed_at=?, attempts=attempts+1
           WHERE id=? AND status='paid'""",
        (now or _now(), order_id),
    )
    return cur.rowcount == 1


def finish_order(order_id, service_id=None, now=None):
    _exec(
        """UPDATE orders SET status='done', finished_at=?,
                             service_id=COALESCE(?, service_id), last_error=NULL
           WHERE id=?""",
        (now or _now(), service_id, order_id),
    )


def set_order_service(order_id, service_id):
    """Tautkan order ke layanan sedini mungkin.

    Penting untuk percobaan ulang: kalau provisioning gagal lalu diulang,
    bot memakai baris layanan yang sama, bukan bikin VPS kedua.
    """
    _exec("UPDATE orders SET service_id=? WHERE id=?", (service_id, order_id))


def release_provision(order_id, error):
    """Kembalikan ke 'paid' supaya dicoba lagi. Uang pelanggan tidak hilang."""
    _exec(
        "UPDATE orders SET status='paid', last_error=? WHERE id=? AND status='provisioning'",
        (str(error)[:500], order_id),
    )


def fail_order(order_id, error, now=None):
    _exec(
        "UPDATE orders SET status='failed', last_error=?, finished_at=? WHERE id=?",
        (str(error)[:500], now or _now(), order_id),
    )


def cancel_order(order_id):
    cur = _exec(
        "UPDATE orders SET status='canceled' WHERE id=? AND status='pending'",
        (order_id,),
    )
    return cur.rowcount == 1


def expire_stale_orders(now=None):
    now = now or _now()
    cur = _exec(
        "UPDATE orders SET status='expired' WHERE status='pending' AND expires_at<=?",
        (now,),
    )
    return cur.rowcount


def stuck_provisioning(older_than_ts):
    return _all(
        "SELECT * FROM orders WHERE status='provisioning' AND claimed_at<=?",
        (older_than_ts,),
    )


# ------------------------------------------------------------------ services
def create_service(tg_id, plan_id, provider, expires_at, now=None):
    now = now or _now()
    cur = _exec(
        """INSERT INTO services (tg_id, plan_id, provider, status, created_at, expires_at)
           VALUES (?,?,?,'provisioning',?,?)""",
        (tg_id, plan_id, provider, now, expires_at),
    )
    return cur.lastrowid


def attach_service_details(service_id, node=None, vmid=None, hostname=None,
                           ip=None, ssh_port=None, username=None, password=None):
    _exec(
        """UPDATE services SET node=COALESCE(?,node), vmid=COALESCE(?,vmid),
                  hostname=COALESCE(?,hostname), ip=COALESCE(?,ip),
                  ssh_port=COALESCE(?,ssh_port), username=COALESCE(?,username),
                  password=COALESCE(?,password)
           WHERE id=?""",
        (node, vmid, hostname, ip, ssh_port, username, password, service_id),
    )


def get_service(service_id):
    return _one("SELECT * FROM services WHERE id=?", (service_id,))


def list_services(tg_id):
    return _all(
        """SELECT * FROM services WHERE tg_id=? AND status!='terminated'
           ORDER BY id DESC""",
        (tg_id,),
    )


def list_by_status(status):
    return _all("SELECT * FROM services WHERE status=? ORDER BY id", (status,))


def set_service_status(service_id, status, now=None, error=None):
    now = now or _now()
    if status == "suspended":
        _exec(
            "UPDATE services SET status=?, suspended_at=?, last_error=? WHERE id=?",
            (status, now, error, service_id),
        )
    elif status == "terminated":
        _exec(
            "UPDATE services SET status=?, terminated_at=?, last_error=? WHERE id=?",
            (status, now, error, service_id),
        )
    else:
        _exec(
            "UPDATE services SET status=?, last_error=? WHERE id=?",
            (status, error, service_id),
        )


def claim_suspend(service_id):
    """Kunci supaya proses stop tidak dijalankan dua kali bersamaan."""
    cur = _exec(
        "UPDATE services SET status='suspending' WHERE id=? AND status='active'",
        (service_id,),
    )
    return cur.rowcount == 1


def claim_terminate(service_id):
    cur = _exec(
        "UPDATE services SET status='terminating' WHERE id=? AND status='suspended'",
        (service_id,),
    )
    return cur.rowcount == 1


def set_expiry(service_id, expires_at, reset_warnings=True):
    if reset_warnings:
        _exec(
            "UPDATE services SET expires_at=?, warn_flags=0 WHERE id=?",
            (expires_at, service_id),
        )
    else:
        _exec("UPDATE services SET expires_at=? WHERE id=?", (expires_at, service_id))


def mark_warned(service_id, flag):
    """Bitmask, jadi tiap tahap peringatan hanya dikirim sekali."""
    cur = _exec(
        "UPDATE services SET warn_flags=warn_flags|? WHERE id=? AND (warn_flags & ?)=0",
        (flag, service_id, flag),
    )
    return cur.rowcount == 1


def due_for_suspend(now=None):
    return _all(
        "SELECT * FROM services WHERE status='active' AND expires_at<=?",
        (now or _now(),),
    )


def due_for_terminate(cutoff):
    return _all(
        """SELECT * FROM services WHERE status='suspended'
           AND suspended_at IS NOT NULL AND suspended_at<=?""",
        (cutoff,),
    )


def active_services(now=None):
    return _all(
        "SELECT * FROM services WHERE status='active' AND expires_at>?",
        (now or _now(),),
    )


# ------------------------------------------------------------------ IP pool
def seed_ip_pool(ips):
    added = 0
    for ip in ips:
        row = _one("SELECT ip FROM ip_pool WHERE ip=?", (ip,))
        if row is None:
            _exec("INSERT INTO ip_pool (ip, service_id) VALUES (?, NULL)", (ip,))
            added += 1
    return added


def alloc_ip(service_id):
    """Ambil satu IP kosong. UPDATE bersyarat supaya tidak bentrok antar thread."""
    with _lock:
        row = _conn.execute(
            "SELECT ip FROM ip_pool WHERE service_id IS NULL ORDER BY ip LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        cur = _conn.execute(
            "UPDATE ip_pool SET service_id=? WHERE ip=? AND service_id IS NULL",
            (service_id, row["ip"]),
        )
        _conn.commit()
        if cur.rowcount == 1:
            return row["ip"]
        return None


def free_ip(service_id):
    _exec("UPDATE ip_pool SET service_id=NULL WHERE service_id=?", (service_id,))


def ip_usage():
    total = _one("SELECT COUNT(*) AS n FROM ip_pool")
    used = _one("SELECT COUNT(*) AS n FROM ip_pool WHERE service_id IS NOT NULL")
    return (used["n"] if used else 0), (total["n"] if total else 0)


# ------------------------------------------------------------------ stats
def stats(now=None):
    now = now or _now()
    revenue = _one("SELECT COALESCE(SUM(amount),0) AS s FROM orders WHERE status='done'")
    used_ip, total_ip = ip_usage()
    return {
        "users": count_users(),
        "revenue": revenue["s"] if revenue else 0,
        "active": len(list_by_status("active")),
        "suspended": len(list_by_status("suspended")),
        "terminated": len(list_by_status("terminated")),
        "pending_orders": len(list_orders_by_status("pending")),
        "failed_orders": len(list_orders_by_status("failed")),
        "ip_used": used_ip,
        "ip_total": total_ip,
    }
