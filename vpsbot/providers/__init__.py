"""Pemilih driver provider berdasarkan konfigurasi."""

from .. import config
from .base import ProvisionError, ProvisionFatal, Provisioner, Spec
from .mock import MockProvisioner


def build(name=None):
    name = (name or config.PROVIDER).lower()

    if name == "mock":
        return MockProvisioner(state_path=config.MOCK_STATE_PATH)

    if name == "proxmox":
        # diimpor di sini supaya mode mock tidak butuh konfigurasi Proxmox
        from .proxmox import ProxmoxProvisioner

        return ProxmoxProvisioner()

    raise ValueError("PROVIDER tidak dikenal: " + str(name))


__all__ = [
    "build",
    "Provisioner",
    "Spec",
    "ProvisionError",
    "ProvisionFatal",
    "MockProvisioner",
]
