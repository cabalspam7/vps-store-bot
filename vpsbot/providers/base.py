"""Kontrak provider. Semua driver VPS harus mengikuti bentuk ini.

Kenapa dipisah: kalau nanti pindah dari Proxmox ke VirtFusion / cloud lain,
yang diganti cuma satu file driver. Logika bisnis (order, expiry, suspend)
tidak perlu disentuh.

Aturan penting untuk semua driver:
- stop() dan destroy() HARUS idempotent. Dipanggil dua kali tidak boleh error,
  karena scheduler bisa mengulang setelah gagal.
- Kalau VPS sudah tidak ada, stop()/destroy() dianggap sukses.
"""


class ProvisionError(Exception):
    """Gagal yang masih bisa dicoba lagi."""


class ProvisionFatal(Exception):
    """Gagal permanen, tidak ada gunanya diulang (misal template tidak ada)."""


class Spec:
    """Permintaan pembuatan VPS."""

    def __init__(self, service_id, hostname, cpu, ram_mb, disk_gb,
                 username, password, ip=None, gateway=None, netmask=24,
                 template_vmid=None):
        self.service_id = service_id
        self.hostname = hostname
        self.cpu = cpu
        self.ram_mb = ram_mb
        self.disk_gb = disk_gb
        self.username = username
        self.password = password
        self.ip = ip
        self.gateway = gateway
        self.netmask = netmask
        self.template_vmid = template_vmid


class Provisioner:
    name = "base"

    def create(self, spec):
        """Bikin dan nyalakan VPS.

        Kembalikan dict: {node, vmid, ip, username, password, ssh_port}
        """
        raise NotImplementedError

    def start(self, service):
        """Nyalakan VPS yang sudah ada. Idempotent."""
        raise NotImplementedError

    def stop(self, service):
        """Matikan VPS. Idempotent. Data tidak dihapus."""
        raise NotImplementedError

    def destroy(self, service):
        """Hapus VPS beserta disknya. Idempotent."""
        raise NotImplementedError

    def status(self, service):
        """'running' | 'stopped' | 'missing' | 'unknown'"""
        raise NotImplementedError
