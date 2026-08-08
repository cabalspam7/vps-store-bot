"""Driver Proxmox VE lewat REST API, autentikasi pakai API token.

Alur create: clone dari template -> set CPU/RAM -> resize disk -> set cloud-init
(user, password, IP) -> start.

Catatan: memakai API token (bukan password) supaya tidak perlu menyimpan
kredensial root dan tidak perlu perpanjang ticket.
"""

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import config
from .base import ProvisionError, ProvisionFatal, Provisioner


class ProxmoxProvisioner(Provisioner):
    name = "proxmox"

    def __init__(self, host=None, node=None, token=None, verify_ssl=None,
                 kind=None, storage=None, disk=None, bridge=None,
                 task_timeout=None):
        self.host = (host or config.PROXMOX_HOST).rstrip("/")
        self.node = node or config.PROXMOX_NODE
        self.token = token or config.PROXMOX_TOKEN
        self.kind = (kind or config.PROXMOX_KIND).lower()
        self.storage = storage or config.PROXMOX_STORAGE
        self.disk = disk or config.PROXMOX_DISK
        self.bridge = bridge or config.PROXMOX_BRIDGE
        self.task_timeout = task_timeout or config.PROXMOX_TASK_TIMEOUT

        verify = config.PROXMOX_VERIFY_SSL if verify_ssl is None else verify_ssl
        if verify:
            self._ssl = ssl.create_default_context()
        else:
            # Proxmox umumnya pakai sertifikat self-signed
            self._ssl = ssl.create_default_context()
            self._ssl.check_hostname = False
            self._ssl.verify_mode = ssl.CERT_NONE

        if self.kind not in ("qemu", "lxc"):
            raise ProvisionFatal("PROXMOX_KIND harus 'qemu' atau 'lxc'")

    # ------------------------------------------------------------------ HTTP
    def _url(self, path):
        return self.host + "/api2/json" + path

    def _request(self, method, path, params=None, timeout=30):
        url = self._url(path)
        data = None
        headers = {"Authorization": "PVEAPIToken=" + self.token}

        if params:
            encoded = urllib.parse.urlencode(params, doseq=True)
            if method == "GET":
                url = url + "?" + encoded
            else:
                data = encoded.encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ssl) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8")[:400]
            except Exception:
                pass
            if exc.code in (401, 403):
                raise ProvisionFatal("Proxmox menolak token (HTTP " + str(exc.code) + ")")
            if exc.code == 500 and "does not exist" in body:
                raise ProvisionFatal("Objek Proxmox tidak ada: " + body)
            raise ProvisionError(
                "Proxmox HTTP " + str(exc.code) + " " + path + " " + body
            )
        except urllib.error.URLError as exc:
            raise ProvisionError("Proxmox tidak bisa dihubungi: " + str(exc.reason))

        if not raw:
            return None
        try:
            return json.loads(raw).get("data")
        except ValueError:
            raise ProvisionError("Respons Proxmox bukan JSON")

    def _base(self, vmid=None):
        path = "/nodes/" + self.node + "/" + self.kind
        if vmid is not None:
            path = path + "/" + str(vmid)
        return path

    # ------------------------------------------------------------------ tasks
    def _wait_task(self, upid):
        """Proxmox mengembalikan UPID untuk operasi async. Tunggu sampai selesai."""
        if not upid:
            return
        deadline = time.time() + self.task_timeout
        path = "/nodes/" + self.node + "/tasks/" + urllib.parse.quote(str(upid)) + "/status"
        while time.time() < deadline:
            info = self._request("GET", path) or {}
            if info.get("status") == "stopped":
                exitstatus = str(info.get("exitstatus", ""))
                if exitstatus == "OK":
                    return
                raise ProvisionError("Task Proxmox gagal: " + exitstatus)
            time.sleep(2)
        raise ProvisionError("Task Proxmox timeout setelah " + str(self.task_timeout) + "s")

    def _next_vmid(self):
        value = self._request("GET", "/cluster/nextid")
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ProvisionError("Gagal ambil VMID berikutnya")

    # ------------------------------------------------------------------ create
    def create(self, spec):
        if not spec.template_vmid:
            raise ProvisionFatal(
                "Paket ini belum punya template_vmid. Set dulu lewat /addplan."
            )

        vmid = self._next_vmid()

        clone_params = {
            "newid": vmid,
            "name" if self.kind == "qemu" else "hostname": spec.hostname,
        }
        if self.kind == "qemu" and config.PROXMOX_FULL_CLONE:
            clone_params["full"] = 1
            clone_params["storage"] = self.storage

        upid = self._request(
            "POST", self._base(spec.template_vmid) + "/clone", clone_params, timeout=60
        )
        self._wait_task(upid)

        try:
            self._configure(vmid, spec)
            self._resize_disk(vmid, spec)
            self._start(vmid)
            ip = spec.ip or self._discover_ip(vmid)
        except Exception:
            # jangan tinggalkan VM setengah jadi yang tetap makan resource
            self._safe_destroy(vmid)
            raise

        return {
            "node": self.node,
            "vmid": vmid,
            "ip": ip,
            "username": spec.username,
            "password": spec.password,
            "ssh_port": config.SSH_PORT,
        }

    def _configure(self, vmid, spec):
        params = {"memory": spec.ram_mb}
        if self.kind == "qemu":
            params["cores"] = spec.cpu
            params["ciuser"] = spec.username
            params["cipassword"] = spec.password
            params["nameserver"] = "1.1.1.1 8.8.8.8"
            if spec.ip and spec.gateway:
                params["ipconfig0"] = (
                    "ip=" + spec.ip + "/" + str(spec.netmask) + ",gw=" + spec.gateway
                )
            else:
                params["ipconfig0"] = "ip=dhcp"
            params["net0"] = "virtio,bridge=" + self.bridge
        else:
            params["cores"] = spec.cpu
            params["hostname"] = spec.hostname
            params["password"] = spec.password
            if spec.ip and spec.gateway:
                netconf = (
                    "name=eth0,bridge=" + self.bridge + ",ip=" + spec.ip
                    + "/" + str(spec.netmask) + ",gw=" + spec.gateway
                )
            else:
                netconf = "name=eth0,bridge=" + self.bridge + ",ip=dhcp"
            params["net0"] = netconf

        self._request("POST", self._base(vmid) + "/config", params, timeout=45)

    def _resize_disk(self, vmid, spec):
        if not spec.disk_gb:
            return
        disk = self.disk if self.kind == "qemu" else "rootfs"
        try:
            # Proxmox hanya mengizinkan pembesaran, bukan pengecilan
            self._request(
                "PUT",
                self._base(vmid) + "/resize",
                {"disk": disk, "size": str(spec.disk_gb) + "G"},
                timeout=60,
            )
        except ProvisionError as exc:
            # template mungkin sudah lebih besar dari paket; jangan gagalkan order
            print("[proxmox] resize dilewati untuk " + str(vmid) + ": " + str(exc))

    def _start(self, vmid):
        upid = self._request("POST", self._base(vmid) + "/status/start", {}, timeout=45)
        self._wait_task(upid)

    def _discover_ip(self, vmid):
        """Mode DHCP: tanya guest agent. Butuh qemu-guest-agent di template."""
        if self.kind != "qemu":
            return None
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                data = self._request(
                    "GET", self._base(vmid) + "/agent/network-get-interfaces"
                )
            except (ProvisionError, ProvisionFatal):
                data = None
            if data:
                for iface in data.get("result", []):
                    if iface.get("name") in ("lo", "lo0"):
                        continue
                    for addr in iface.get("ip-addresses", []):
                        ip = addr.get("ip-address", "")
                        if addr.get("ip-address-type") == "ipv4" and not ip.startswith("127."):
                            return ip
            time.sleep(5)
        return None

    def _safe_destroy(self, vmid):
        try:
            self._request("POST", self._base(vmid) + "/status/stop", {}, timeout=45)
        except Exception:
            pass
        try:
            time.sleep(3)
            self._request("DELETE", self._base(vmid), {"purge": 1}, timeout=60)
        except Exception as exc:
            print("[proxmox] gagal bersihkan VM " + str(vmid) + ": " + str(exc))

    # ------------------------------------------------------------ lifecycle
    def start(self, service):
        vmid = self._service_vmid(service)
        state = self.status(service)
        if state == "missing":
            raise ProvisionError("VPS " + str(vmid) + " tidak ada di Proxmox")
        if state == "running":
            return True
        self._start(vmid)
        return True

    def stop(self, service):
        """Idempotent: kalau sudah mati atau sudah hilang, dianggap sukses."""
        vmid = self._service_vmid(service)
        state = self.status(service)
        if state in ("stopped", "missing"):
            return True
        upid = self._request("POST", self._base(vmid) + "/status/stop", {}, timeout=45)
        self._wait_task(upid)
        return True

    def destroy(self, service):
        """Idempotent: hapus VM beserta disk. Sudah hilang = sukses."""
        vmid = self._service_vmid(service)
        if self.status(service) == "missing":
            return True
        try:
            self.stop(service)
        except ProvisionError:
            pass
        upid = self._request(
            "DELETE", self._base(vmid), {"purge": 1, "destroy-unreferenced-disks": 1},
            timeout=90,
        )
        self._wait_task(upid)
        return True

    def status(self, service):
        vmid = self._service_vmid(service)
        try:
            info = self._request("GET", self._base(vmid) + "/status/current") or {}
        except ProvisionFatal as exc:
            if "tidak ada" in str(exc):
                return "missing"
            raise
        except ProvisionError as exc:
            if "HTTP 500" in str(exc) or "HTTP 404" in str(exc):
                return "missing"
            raise
        state = str(info.get("status", "")).lower()
        if state in ("running", "stopped"):
            return state
        return "unknown"

    @staticmethod
    def _service_vmid(service):
        try:
            return int(service["vmid"])
        except (KeyError, TypeError, ValueError):
            raise ProvisionFatal("Layanan ini belum punya VMID")
