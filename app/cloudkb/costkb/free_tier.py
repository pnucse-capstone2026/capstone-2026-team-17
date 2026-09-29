"""Curated VM Free Tier policy facts for AWS, Azure, and GCP."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_POLICY_PATH = Path(__file__).with_name("free_tier.json")
_SCHEMA_VERSION = "easydep-vm-free-tier/v1"
_PROVIDERS = frozenset({"aws", "azure", "gcp"})

_ELIGIBLE_AVAILABILITY = frozenset(
    {"all-current-account-cohorts", "all-free-accounts", "always-free"}
)
_LABELS = {
    "eligible": "VM Free Tier eligible",
    "conditional": "VM Free Tier: account-dependent",
    "notEligible": "Not listed for VM Free Tier",
    "unknown": "VM Free Tier status unknown",
}


@lru_cache(maxsize=1)
def load_free_tier_policy() -> dict[str, Any]:
    """Load the reviewed policy file and fail clearly if its contract is damaged."""

    data = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    if data.get("schemaVersion") != _SCHEMA_VERSION:
        raise ValueError("Unsupported VM Free Tier policy schema.")
    providers = data.get("providers")
    if not isinstance(providers, dict) or set(providers) != _PROVIDERS:
        raise ValueError("VM Free Tier policy must define AWS, Azure, and GCP exactly once.")
    for provider, offer in providers.items():
        if not isinstance(offer, dict) or not isinstance(offer.get("skus"), dict):
            raise TypeError(f"VM Free Tier policy for {provider} has no SKU map.")
        if not offer.get("sources") or not offer.get("conditions"):
            raise ValueError(f"VM Free Tier policy for {provider} has no evidence or limits.")
    return data


def _sku_rule(provider: str, region: str, sku: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    policy = load_free_tier_policy()
    offer = policy["providers"][provider]
    rule = offer["skus"].get(sku.strip().lower())
    if not isinstance(rule, dict):
        return offer, None
    eligible_regions = {str(item).lower() for item in rule.get("regions") or []}
    if eligible_regions and region.strip().lower() not in eligible_regions:
        return offer, None
    return offer, rule


def vm_free_tier_status(*, provider: str, region: str, sku: str) -> dict[str, Any]:
    """Return SKU/region eligibility without implying that an account will pay zero."""

    policy = load_free_tier_policy()
    provider = provider.strip().lower()
    if provider not in _PROVIDERS:
        return {
            "status": "unknown",
            "label": _LABELS["unknown"],
            "summary": "No reviewed VM Free Tier policy is available for this provider.",
            "conditions": [],
            "sourceUrls": [],
            "asOf": policy["asOf"],
        }

    offer, rule = _sku_rule(provider, region, sku)
    if rule is None:
        status = "notEligible"
        summary = (
            f"This SKU and region are not listed in {offer['program']} as of {policy['asOf']}."
        )
    else:
        availability = str(rule.get("availability") or "")
        status = "eligible" if availability in _ELIGIBLE_AVAILABILITY else "conditional"
        if status == "eligible":
            summary = (
                f"Listed in {offer['program']}; eligibility and usage limits apply, and other "
                "deployment resources may still incur charges."
            )
        else:
            summary = (
                f"Listed in {offer['program']} only for {availability.replace('-', ' ')}."
            )
    return {
        "status": status,
        "label": _LABELS[status],
        "summary": summary,
        "conditions": list(offer["conditions"]),
        "sourceUrls": [str(item["url"]) for item in offer["sources"]],
        "asOf": policy["asOf"],
    }


def preferred_vm_free_tier_skus(*, provider: str, region: str) -> list[str]:
    """Return universally listed SKU names worth keeping in a short candidate list."""

    provider = provider.strip().lower()
    policy = load_free_tier_policy()
    offer = policy["providers"].get(provider)
    if not isinstance(offer, dict):
        return []
    return [
        sku
        for sku in offer["skus"]
        if vm_free_tier_status(provider=provider, region=region, sku=sku)["status"] == "eligible"
    ]


def vm_free_tier_notice() -> dict[str, str]:
    """Return the scope warning shown with sizing choices."""

    policy = load_free_tier_policy()
    return {
        "asOf": str(policy["asOf"]),
        "scope": str(policy["scope"]),
        "disclaimer": str(policy["disclaimer"]),
    }


__all__ = [
    "load_free_tier_policy",
    "preferred_vm_free_tier_skus",
    "vm_free_tier_notice",
    "vm_free_tier_status",
]
