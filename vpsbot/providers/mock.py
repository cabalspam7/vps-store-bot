"""Driver simulasi. Untuk tes alur lengkap tanpa server Proxmox sungguhan.

State disimpan ke file JSON, jadi tetap konsisten walau bot direstart.
Bisa juga dipakai untuk latihan sebelum menyentuh server produksi.
"""

import json
import os
import threading

from .base import ProvisionError, Provisioner


class MockProvisioner(Provisioner):
    name = "mock"

    def __init__(self, state_path="mock_provider.json", fail_create=False,
                 fail_stop=False):
        self.state_path = state_path
        self.fail_create = fail_create
        self.fail_stop = fail_stop
        self._lock = threading.RLock()
        self._vms = {}
        self._next_vmid = 9000
        self._load()

    # ------------------------------------------------------------ persistence
    def _load(self):
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self._vms = {int(k): v for k, v in data.get("vms", {}).items()}
            self._next_vmid = int(data.get("next_vmid", 9000))
        except (ValueError, OSError):
            self._vms = {}

    def _save(self):
        if not self.state_path:
            return
        folder = os.path.dirname(os.path.abspath(self.state_path))
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(
                {"vms": {str(k): v for k, v in self._vms.items()},
                 "next_vmid": self._next_vmid},
                handle,
            )
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------ operations
    def create(self, spec):
        if self.fail_create:
            raise ProvisionError("simulasi gagal create")
        with self._lock:
            vmid = self._next_vmid
            self._next_vmid += 1
            ip = spec.ip or ("10.0.0." + str(vmid % 250 + 2))
            self._vms[vmid] = {
                "state": "running",
                "hostname": spec.hostname,
                "cpu": spec.cpu,
                "ram_mb": spec.ram_mb,
                "disk_gb": spec.disk_gb,
                "ip": ip,
            }
            self._save()
        return {
            "node": "mock-node",
            "vmid": vmid,
            "ip": ip,
            "username": spec.username,
            "password": spec.password,
            "ssh_port": 22,
        }

    def start(self, service):
        with self._lock:
            vm = self._vms.get(self._vmid(service))
            if vm is None:
                raise ProvisionError("VPS tidak ditemukan di provider")
            vm["state"] = "running"
            self._save()
        return True

    def stop(self, service):
        if self.fail_stop:
            raise ProvisionError("simulasi gagal stop")
        with self._lock:
            vm = self._vms.get(self._vmid(service))
            if vm is None:
                return True  # sudah tidak ada, anggap berhasil
            vm["state"] = "stopped"
            self._save()
        return True

    def destroy(self, service):
        with self._lock:
            self._vms.pop(self._vmid(service), None)
            self._save()
        return True

    def status(self, service):
        vm = self._vms.get(self._vmid(service))
        if vm is None:
            return "missing"
        return vm["state"]

    @staticmethod
    def _vmid(service):
        try:
            return int(service["vmid"])
        except (KeyError, TypeError, ValueError):
            return -1
