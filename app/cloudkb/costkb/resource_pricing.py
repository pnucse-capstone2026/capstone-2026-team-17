"""Official-source pricing rules for resources used by Docker-on-VM plans.

The bundled rules describe billing equations and the dimensions needed to look
up current retail rates.  They intentionally do not execute the equations or
bundle a mutable copy of every provider price.  A caller must supply current
rates and real configuration/usage inputs; missing usage is unknown, never zero.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import jsonschema

_RULES_PATH = Path(__file__).with_name("resource_pricing_rules.json")
_SCHEMA_PATH = Path(__file__).with_name("resource_pricing_schema.json")


@lru_cache(maxsize=1)
def _schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _validate_semantics(data: dict) -> None:
    sources = set(data["sources"])
    for provider, rate_source in data["rateSources"].items():
        if rate_source["sourceRef"] not in sources:
            raise ValueError(f"Unknown {provider} rate source: {rate_source['sourceRef']}")

    rule_ids: set[str] = set()
    primitive_owners: dict[tuple[str, str], str] = {}
    for rule in data["rules"]:
        rule_id = rule["id"]
        if rule_id in rule_ids:
            raise ValueError(f"Duplicate resource pricing rule: {rule_id}")
        rule_ids.add(rule_id)
        if not rule_id.startswith(f"{rule['provider']}."):
            raise ValueError(f"Rule/provider mismatch: {rule_id}/{rule['provider']}")

        missing_sources = set(rule["sourceRefs"]) - sources
        if missing_sources:
            raise ValueError(f"Unknown source refs in {rule_id}: {sorted(missing_sources)}")

        rate_keys = {item["key"] for item in rule["rateLookups"]}
        used_rate_keys = {item["rateKey"] for item in rule["billingTerms"]}
        if rate_keys != used_rate_keys:
            raise ValueError(
                f"Rate keys do not match billing terms in {rule_id}: "
                f"declared={sorted(rate_keys)}, used={sorted(used_rate_keys)}"
            )

        for primitive in rule["providerPrimitiveKinds"]:
            key = (rule["provider"], primitive)
            previous = primitive_owners.get(key)
            if previous is not None:
                raise ValueError(
                    f"Pricing ownership overlaps for {key}: {previous}, {rule_id}"
                )
            primitive_owners[key] = rule_id

    for provider, coverage in data["coverage"].items():
        for primitive in coverage["noSeparateMeterPrimitiveKinds"]:
            owner = primitive_owners.get((provider, primitive))
            if owner is not None:
                raise ValueError(
                    f"{provider}/{primitive} is both metered by {owner} and "
                    "classified as having no separate meter"
                )


@lru_cache(maxsize=1)
def load_resource_pricing_rules() -> dict:
    """Load and validate the bundled pricing-rule knowledge base."""

    data = json.loads(_RULES_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(data, _schema())
    _validate_semantics(data)
    return data


def resource_pricing_rules(
    *, provider: str | None = None, category: str | None = None
) -> list[dict]:
    """Return rules, optionally restricted by provider and billing category."""

    rules = load_resource_pricing_rules()["rules"]
    wanted_provider = provider.strip().lower() if provider else None
    wanted_category = category.strip().lower() if category else None
    return [
        rule
        for rule in rules
        if (wanted_provider is None or rule["provider"] == wanted_provider)
        and (wanted_category is None or rule["category"] == wanted_category)
    ]


def resource_pricing_rule_for_primitive(provider: str, primitive_kind: str) -> dict | None:
    """Return the sole billing owner for a provider primitive, if it has one."""

    wanted_provider = provider.strip().lower()
    wanted_primitive = primitive_kind.strip()
    for rule in resource_pricing_rules(provider=wanted_provider):
        if wanted_primitive in rule["providerPrimitiveKinds"]:
            return rule
    return None


def no_separate_meter_primitive_kinds(provider: str) -> frozenset[str]:
    """Return structural primitives with no distinct meter in this KB's scope."""

    coverage = load_resource_pricing_rules()["coverage"].get(provider.strip().lower())
    if not isinstance(coverage, dict):
        return frozenset()
    return frozenset(coverage["noSeparateMeterPrimitiveKinds"])


def clear_resource_pricing_cache() -> None:
    """Clear caches for tests and local knowledge-base refreshes."""

    load_resource_pricing_rules.cache_clear()
    _schema.cache_clear()


__all__ = [
    "clear_resource_pricing_cache",
    "load_resource_pricing_rules",
    "no_separate_meter_primitive_kinds",
    "resource_pricing_rule_for_primitive",
    "resource_pricing_rules",
]
