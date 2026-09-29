"""Authoritative Docker-on-VM boundary for dependency knowledge."""

from __future__ import annotations

VM_RESOURCE_TYPES = frozenset(
    {
        "network",
        "subnet",
        "firewall",
        "nic",
        "publicIp",
        "publicIPPrefix",  # Azure form; normalized as publicIp in evaluation.
        "loadBalancer",
        "vm",
        "disk",
        "sshKey",
        "workloadIdentity",
        "defaultRoute",
    }
)

VM_ANCHOR_TYPES = frozenset(
    {"network", "subnet", "firewall", "publicIp", "loadBalancer", "vm", "disk"}
)


def is_vm_claim(claim: dict) -> bool:
    """Return whether both ends of a claim belong to the VM deployment scope."""
    subject = str(claim.get("subject") or "")
    objects = str(claim.get("object") or "").split("|")
    return subject in VM_RESOURCE_TYPES and bool(objects) and all(
        item in VM_RESOURCE_TYPES for item in objects
    )
