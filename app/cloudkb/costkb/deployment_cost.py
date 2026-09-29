"""Typed, offline deployment-cost calculation from the bundled pricing rules."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from .resource_pricing import (
    no_separate_meter_primitive_kinds,
    resource_pricing_rule_for_primitive,
)

HOURS_PER_MONTH = Decimal(730)
SECONDS_PER_HOUR = Decimal(3600)


class UsageScenario(BaseModel):
    """One generated resource or generated-usage meter and its known inputs."""

    primitive_kind: str
    values: dict[str, Any] = Field(default_factory=dict)
    label: str | None = None


class PricingInventory(BaseModel):
    provider: Literal["aws", "azure", "gcp"]
    scenarios: list[UsageScenario]
    monthly_budget_usd: Decimal | None = Field(default=None, ge=0)
    hours_per_month: Decimal = Field(default=HOURS_PER_MONTH, gt=0)


class RateTier(BaseModel):
    start_units: Decimal = Field(ge=0)
    end_units: Decimal | None = Field(default=None, gt=0)
    unit_rate_usd: Decimal = Field(ge=0)


class RateQuote(BaseModel):
    rate_key: str
    unit_rate_usd: Decimal | None = Field(default=None, ge=0)
    units_per_rate: Decimal = Field(default=Decimal(1), gt=0)
    tiers: list[RateTier] = Field(default_factory=list)
    currency: Literal["USD"] = "USD"
    source_ref: str | None = None
    effective_at: datetime | None = None
    snapshot_digest: str | None = None
    stale: bool = False
    source: str | None = None
    unknown_reason: str | None = None


class RetailRateResolver(Protocol):
    def resolve_rate(
        self,
        *,
        provider: str,
        rule_id: str,
        rate_key: str,
        dimensions: Mapping[str, Any],
    ) -> RateQuote | None: ...


class CostComponent(BaseModel):
    rule_id: str
    primitive_kind: str
    term: str
    amount_usd: Decimal | None
    rate_key: str | None = None
    known: bool
    reason: str | None = None


class BudgetVerdict(StrEnum):
    EXCEEDS = "exceeds"
    WITHIN = "within"
    INDETERMINATE = "indeterminate"
    NOT_PROVIDED = "notProvided"


class DeploymentCostQuote(BaseModel):
    currency: Literal["USD"] = "USD"
    components: list[CostComponent]
    known_floor_usd: Decimal
    complete: bool
    monthly_budget_usd: Decimal | None
    verdict: BudgetVerdict


_Term = tuple[str, str, Decimal | None, str | None]


def _decimal(
    values: Mapping[str, Any], name: str, default: Decimal | None = None
) -> Decimal | None:
    value = values.get(name, default)
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _ceil(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding=ROUND_CEILING)


def _month_fraction(seconds: Decimal, hours_per_month: Decimal) -> Decimal:
    return seconds / (hours_per_month * SECONDS_PER_HOUR)


def _term_from_inputs(
    term: str,
    rate_key: str,
    inputs: Mapping[str, Decimal | None],
    calculate: Callable[[], Decimal],
) -> _Term:
    """Keep independently calculable billing terms when sibling usage is absent."""

    missing = [name for name, value in inputs.items() if value is None]
    if missing:
        return (
            term,
            rate_key,
            None,
            f"Missing required usage/configuration: {', '.join(missing)}.",
        )
    return term, rate_key, calculate(), None


def _terms_instance_runtime(
    rule: Mapping[str, Any], values: Mapping[str, Any], hours: Decimal
) -> list[_Term]:
    count = _decimal(values, "instance_count")
    if count is None:
        return []
    if rule["provider"] == "gcp":
        units = _decimal(values, "running_seconds")
        if units is None:
            return []
        units = (
            max(units, Decimal(str(rule["parameters"].get("minimumSecondsPerLaunch", 0))))
            / SECONDS_PER_HOUR
        )
        model = str(values.get("pricing_model") or "")
        if model == "resource-based":
            vcpu = _decimal(values, "vcpu_count")
            memory = _decimal(values, "memory_gib")
            return [
                _term_from_inputs(
                    "vcpu_runtime",
                    "vcpu_runtime_rate",
                    {"vcpu_count": vcpu},
                    lambda: count * vcpu * units,
                ),
                _term_from_inputs(
                    "memory_runtime",
                    "memory_gib_runtime_rate",
                    {"memory_gib": memory},
                    lambda: count * memory * units,
                ),
            ]
        if model == "shared-core-instance":
            return [
                ("shared_core_runtime", "shared_core_instance_runtime_rate", count * units, None)
            ]
        return [("instance_runtime", "", None, "Unknown GCP pricing_model.")]
    if rule["provider"] == "aws":
        seconds = _decimal(values, "running_seconds")
        if seconds is None:
            return []
        units = (
            max(seconds, Decimal(str(rule["parameters"].get("linuxMinimumSecondsPerLaunch", 0))))
            / SECONDS_PER_HOUR
        )
    else:
        units = _decimal(values, "running_hours")
        if units is None:
            return []
    return [
        (
            "runtime",
            "instance_runtime_rate" if rule["provider"] == "aws" else "vm_runtime_rate",
            count * units,
            None,
        )
    ]


def _terms_gp3(
    rule: Mapping[str, Any], values: Mapping[str, Any], hours: Decimal
) -> list[_Term]:
    count, capacity, seconds = (
        _decimal(values, key) for key in ("volume_count", "capacity_gib", "provisioned_seconds")
    )
    included_iops = Decimal(str(rule["parameters"]["includedIops"]))
    included_throughput = Decimal(str(rule["parameters"]["includedThroughputMiBps"]))
    iops = _decimal(values, "provisioned_iops", Decimal(0))
    throughput = _decimal(values, "provisioned_throughput_mibps", Decimal(0))
    return [
        _term_from_inputs(
            "capacity",
            "storage_gib_month_rate",
            {"volume_count": count, "capacity_gib": capacity, "provisioned_seconds": seconds},
            lambda: count * capacity * _month_fraction(seconds, hours),
        ),
        _term_from_inputs(
            "extra_iops",
            "extra_iops_month_rate",
            {"volume_count": count, "provisioned_seconds": seconds},
            lambda: count
            * max(Decimal(0), iops - included_iops)
            * _month_fraction(seconds, hours),
        ),
        _term_from_inputs(
            "extra_throughput",
            "extra_throughput_month_rate",
            {"volume_count": count, "provisioned_seconds": seconds},
            lambda: count
            * max(Decimal(0), throughput - included_throughput)
            * _month_fraction(seconds, hours),
        ),
    ]


def _terms_nlb(rule: Mapping[str, Any], values: Mapping[str, Any], _: Decimal) -> list[_Term]:
    hours = _decimal(values, "load_balancer_hours")
    new, active, processed = (
        _decimal(values, key)
        for key in ("new_flows_per_second", "active_flows", "processed_gib_per_hour")
    )
    addresses = _decimal(values, "public_ipv4_count")
    protocol = str(values.get("protocol") or "").lower()
    capacity = rule["parameters"].get("capacityPerNlcu", {}).get(protocol)
    capacity_inputs = {
        "load_balancer_hours": hours,
        "new_flows_per_second": new,
        "active_flows": active,
        "processed_gib_per_hour": processed,
        "protocol": Decimal(1) if isinstance(capacity, dict) else None,
    }
    return [
        _term_from_inputs(
            "load_balancer_runtime",
            "load_balancer_hour_rate",
            {"load_balancer_hours": hours},
            lambda: hours,
        ),
        _term_from_inputs(
            "capacity",
            "nlcu_hour_rate",
            capacity_inputs,
            lambda: max(
                new / Decimal(str(capacity["newFlowsPerSecond"])),
                active / Decimal(str(capacity["activeFlows"])),
                processed / Decimal(str(capacity["processedGiBPerHour"])),
            )
            * hours,
        ),
        _term_from_inputs(
            "implicit_public_ipv4",
            "public_ipv4_hour_rate",
            {"public_ipv4_count": addresses, "load_balancer_hours": hours},
            lambda: addresses * hours,
        ),
    ]


def _terms_gateway(_: Mapping[str, Any], values: Mapping[str, Any], __: Decimal) -> list[_Term]:
    hours, data = (_decimal(values, key) for key in ("gateway_hours", "processed_gb"))
    return [
        _term_from_inputs(
            "gateway_runtime",
            "gateway_hour_rate",
            {"gateway_hours": hours},
            lambda: _ceil(hours),
        ),
        _term_from_inputs(
            "data_processing",
            "processed_gb_rate",
            {"processed_gb": data},
            lambda: data,
        ),
    ]


def _terms_address(rule: Mapping[str, Any], values: Mapping[str, Any], _: Decimal) -> list[_Term]:
    count, hours = (_decimal(values, key) for key in ("address_count", "allocated_hours"))
    if count is None or hours is None:
        return []
    units = count * (_ceil(hours) if rule["provider"] == "azure" else hours)
    return [("address_runtime", "public_ipv4_hour_rate", units, None)]


def _terms_registry_aws(
    _: Mapping[str, Any], values: Mapping[str, Any], __: Decimal
) -> list[_Term]:
    storage = _decimal(values, "stored_gb_month")
    transfer = _decimal(values, "eligible_transfer_out_gb")
    free = _decimal(values, "account_remaining_free_storage_gb", Decimal(0))
    return [
        _term_from_inputs(
            "storage",
            "registry_storage_rate",
            {"stored_gb_month": storage},
            lambda: max(Decimal(0), storage - free),
        ),
        _term_from_inputs(
            "transfer",
            "registry_transfer_rate",
            {"eligible_transfer_out_gb": transfer},
            lambda: transfer,
        ),
    ]


def _terms_tiered(rule: Mapping[str, Any], values: Mapping[str, Any], _: Decimal) -> list[_Term]:
    keys = {
        "aws": "account_aggregated_eligible_egress_gb",
        "azure": "internet_egress_gb",
        "gcp": "internet_egress_gib",
    }
    volume = _decimal(values, keys[rule["provider"]])
    free = _decimal(values, "account_remaining_free_egress_gb", Decimal(0))
    if volume is None:
        return []
    return [("tiered_egress", "internet_egress_tier_rate", max(Decimal(0), volume - free), None)]


def _terms_api(_: Mapping[str, Any], values: Mapping[str, Any], __: Decimal) -> list[_Term]:
    for field, rate in (
        ("api_calls", "secret_api_call_rate"),
        ("secret_operations", "secret_operation_rate"),
        ("access_operations", "access_operation_rate"),
    ):
        operations = _decimal(values, field)
        if operations is not None:
            free = _decimal(values, "account_remaining_free_access_operations", Decimal(0))
            return [("access_operations", rate, max(Decimal(0), operations - free), None)]
    return []


def _terms_azure_disk(
    _: Mapping[str, Any], values: Mapping[str, Any], hours: Decimal
) -> list[_Term]:
    count, seconds, batches = (
        _decimal(values, key)
        for key in (
            "disk_count",
            "provisioned_seconds",
            "billable_transaction_batches_after_hourly_cap",
        )
    )
    return [
        _term_from_inputs(
            "capacity_tier",
            "disk_tier_month_rate",
            {"disk_count": count, "provisioned_seconds": seconds},
            lambda: count * _month_fraction(seconds, hours),
        ),
        _term_from_inputs(
            "transactions",
            "transaction_batch_rate",
            {"billable_transaction_batches_after_hourly_cap": batches},
            lambda: batches,
        ),
    ]


def _terms_lb_rules(_: Mapping[str, Any], values: Mapping[str, Any], __: Decimal) -> list[_Term]:
    hours, rules, data = (
        _decimal(values, key)
        for key in ("load_balancer_hours", "billable_rule_count", "processed_gb")
    )
    return [
        _term_from_inputs(
            "first_five_rules",
            "first_five_rules_hour_rate",
            {"load_balancer_hours": hours, "billable_rule_count": rules},
            lambda: hours if rules > 0 else Decimal(0),
        ),
        _term_from_inputs(
            "additional_rules",
            "additional_rule_hour_rate",
            {"load_balancer_hours": hours, "billable_rule_count": rules},
            lambda: max(Decimal(0), rules - 5) * hours,
        ),
        _term_from_inputs(
            "data_processing",
            "processed_gb_rate",
            {"processed_gb": data},
            lambda: data,
        ),
    ]


def _terms_registry_azure(
    rule: Mapping[str, Any], values: Mapping[str, Any], _: Decimal
) -> list[_Term]:
    days, stored = (_decimal(values, key) for key in ("registry_days", "stored_gib"))
    sku = str(values.get("sku") or "")
    included = rule["parameters"].get("includedStorageGiB", {}).get(sku)
    return [
        _term_from_inputs(
            "tier_runtime",
            "registry_day_rate",
            {"registry_days": days},
            lambda: days,
        ),
        _term_from_inputs(
            "excess_storage",
            "excess_storage_gib_day_rate",
            {
                "registry_days": days,
                "stored_gib": stored,
                "sku": Decimal(1) if included is not None else None,
            },
            lambda: days * max(Decimal(0), stored - Decimal(str(included))),
        ),
    ]


def _terms_gcp_disk(_: Mapping[str, Any], values: Mapping[str, Any], hours: Decimal) -> list[_Term]:
    count, capacity, seconds = (
        _decimal(values, key) for key in ("disk_count", "capacity_gib", "provisioned_seconds")
    )
    if None in {count, capacity, seconds}:
        return []
    return [
        ("capacity", "disk_gib_time_rate", count * capacity * _month_fraction(seconds, hours), None)
    ]


def _terms_forwarding(_: Mapping[str, Any], values: Mapping[str, Any], __: Decimal) -> list[_Term]:
    count, hours, inbound, outbound = (
        _decimal(values, key)
        for key in (
            "forwarding_rule_count",
            "forwarding_rule_hours",
            "inbound_processed_gib",
            "outbound_processed_gib",
        )
    )
    return [
        _term_from_inputs(
            "first_five_forwarding_rules",
            "first_five_forwarding_rules_hour_rate",
            {"forwarding_rule_count": count, "forwarding_rule_hours": hours},
            lambda: hours if count > 0 else Decimal(0),
        ),
        _term_from_inputs(
            "additional_forwarding_rules",
            "additional_forwarding_rule_hour_rate",
            {"forwarding_rule_count": count, "forwarding_rule_hours": hours},
            lambda: max(Decimal(0), count - 5) * hours,
        ),
        _term_from_inputs(
            "inbound_processing",
            "inbound_processed_gib_rate",
            {"inbound_processed_gib": inbound},
            lambda: inbound,
        ),
        _term_from_inputs(
            "outbound_processing",
            "outbound_processed_gib_rate",
            {"outbound_processed_gib": outbound},
            lambda: outbound,
        ),
    ]


def _terms_gcp_nat(rule: Mapping[str, Any], values: Mapping[str, Any], _: Decimal) -> list[_Term]:
    count, hours, data, addresses = (
        _decimal(values, key)
        for key in ("assigned_vm_count", "gateway_hours", "processed_gib", "external_ip_count")
    )
    cap = Decimal(str(rule["parameters"]["assignedVmCap"]))
    return [
        _term_from_inputs(
            "gateway_runtime_below_cap",
            "per_vm_gateway_hour_rate",
            {"assigned_vm_count": count, "gateway_hours": hours},
            lambda: count * hours if count <= cap else Decimal(0),
        ),
        _term_from_inputs(
            "gateway_runtime_at_cap",
            "capped_gateway_hour_rate",
            {"assigned_vm_count": count, "gateway_hours": hours},
            lambda: hours if count > cap else Decimal(0),
        ),
        _term_from_inputs(
            "data_processing",
            "processed_gib_rate",
            {"processed_gib": data},
            lambda: data,
        ),
        _term_from_inputs(
            "implicit_external_ips",
            "nat_external_ip_hour_rate",
            {"external_ip_count": addresses, "gateway_hours": hours},
            lambda: addresses * hours,
        ),
    ]


def _terms_gcp_address(_: Mapping[str, Any], values: Mapping[str, Any], __: Decimal) -> list[_Term]:
    count, hours = (_decimal(values, key) for key in ("address_count", "allocated_hours"))
    free = _decimal(values, "account_remaining_free_address_hours", Decimal(0))
    if count is None or hours is None:
        return []
    return [
        ("address_runtime", "external_ipv4_hour_rate", max(Decimal(0), count * hours - free), None)
    ]


def _terms_registry_gcp(
    _: Mapping[str, Any], values: Mapping[str, Any], __: Decimal
) -> list[_Term]:
    storage, transfer = (_decimal(values, key) for key in ("stored_gib_month", "transfer_out_gib"))
    free = _decimal(values, "account_remaining_free_storage_gib", Decimal(0))
    return [
        _term_from_inputs(
            "storage",
            "artifact_storage_rate",
            {"stored_gib_month": storage},
            lambda: max(Decimal(0), storage - free),
        ),
        _term_from_inputs(
            "transfer",
            "artifact_transfer_rate",
            {"transfer_out_gib": transfer},
            lambda: transfer,
        ),
    ]


_EVALUATORS = {
    "instance-runtime": _terms_instance_runtime,
    "provisioned-gp3-storage": _terms_gp3,
    "network-load-balancer-capacity": _terms_nlb,
    "gateway-hours-and-processed-data": _terms_gateway,
    "address-hours": _terms_address,
    "registry-storage-and-transfer": _terms_registry_aws,
    "account-aggregated-tiered-transfer": _terms_tiered,
    "api-operation-batches": _terms_api,
    "managed-disk-tier-and-transactions": _terms_azure_disk,
    "load-balancer-rules-and-data": _terms_lb_rules,
    "registry-tier-days-and-excess-storage": _terms_registry_azure,
    "tiered-transfer-by-source-continent": _terms_tiered,
    "provisioned-persistent-disk-storage": _terms_gcp_disk,
    "forwarding-rules-and-data": _terms_forwarding,
    "assigned-vm-capped-gateway-and-data": _terms_gcp_nat,
    "address-hours-by-attachment-state": _terms_gcp_address,
    "registry-storage-and-location-transfer": _terms_registry_gcp,
    "tiered-transfer-by-source-destination-and-network-tier": _terms_tiered,
}
SUPPORTED_CALCULATION_KINDS = frozenset(_EVALUATORS)


def _tiered_amount(units: Decimal, quote: RateQuote) -> Decimal | None:
    if not quote.tiers:
        return (
            units * quote.unit_rate_usd / quote.units_per_rate
            if quote.unit_rate_usd is not None
            else None
        )
    total = Decimal(0)
    for tier in sorted(quote.tiers, key=lambda item: item.start_units):
        upper = tier.end_units if tier.end_units is not None else units
        overlap = max(Decimal(0), min(units, upper) - tier.start_units)
        total += overlap * tier.unit_rate_usd / quote.units_per_rate
    return total


def _unknown(rule_id: str, primitive: str, term: str, reason: str) -> CostComponent:
    return CostComponent(
        rule_id=rule_id,
        primitive_kind=primitive,
        term=term,
        amount_usd=None,
        known=False,
        reason=reason,
    )


def _rate_dimensions(
    rule: Mapping[str, Any], rate_key: str, values: Mapping[str, Any]
) -> dict[str, Any]:
    lookup = next(item for item in rule["rateLookups"] if item["key"] == rate_key)
    overrides = values.get("rate_dimensions")
    per_rate = overrides.get(rate_key) if isinstance(overrides, Mapping) else None
    return {
        name: (
            per_rate.get(name, values.get(name))
            if isinstance(per_rate, Mapping)
            else values.get(name)
        )
        for name in lookup["dimensions"]
    }


def _missing_rate_dimensions(dimensions: Mapping[str, Any]) -> list[str]:
    return sorted(
        name
        for name, value in dimensions.items()
        if value is None or (isinstance(value, str) and not value.strip())
    )


def _evaluate_scenario(
    inventory: PricingInventory, scenario: UsageScenario, resolver: RetailRateResolver
) -> list[CostComponent]:
    primitive = scenario.primitive_kind
    if primitive in no_separate_meter_primitive_kinds(inventory.provider):
        return []
    rule = resource_pricing_rule_for_primitive(inventory.provider, primitive)
    if rule is None:
        return [
            _unknown(
                "unowned", primitive, "unpriced", "No pricing owner is defined for this primitive."
            )
        ]
    evaluator = _EVALUATORS.get(rule["calculationKind"])
    if evaluator is None:
        return [
            _unknown(
                rule["id"],
                primitive,
                str(term["name"]),
                f"Unsupported calculation kind: {rule['calculationKind']}.",
            )
            for term in rule["billingTerms"]
        ]
    terms = evaluator(rule, scenario.values, inventory.hours_per_month)
    if not terms:
        return [
            _unknown(
                rule["id"],
                primitive,
                str(term["name"]),
                "Usage cannot be calculated from supplied inputs.",
            )
            for term in rule["billingTerms"]
        ]
    components: list[CostComponent] = []
    for term, rate_key, units, reason in terms:
        if units is None or not rate_key:
            components.append(
                _unknown(
                    rule["id"],
                    primitive,
                    term,
                    reason or "Usage cannot be calculated from supplied inputs.",
                )
            )
            continue
        dimensions = _rate_dimensions(rule, rate_key, scenario.values)
        missing_dimensions = _missing_rate_dimensions(dimensions)
        if missing_dimensions:
            components.append(
                _unknown(
                    rule["id"],
                    primitive,
                    term,
                    "Missing rate lookup dimensions: "
                    f"{', '.join(missing_dimensions)}.",
                )
            )
            continue
        if units < 0:
            components.append(
                _unknown(rule["id"], primitive, term, "Negative usage is not supported.")
            )
            continue
        if units == 0:
            components.append(
                CostComponent(
                    rule_id=rule["id"],
                    primitive_kind=primitive,
                    term=term,
                    amount_usd=Decimal(0),
                    rate_key=rate_key,
                    known=True,
                )
            )
            continue
        quote = resolver.resolve_rate(
            provider=inventory.provider,
            rule_id=rule["id"],
            rate_key=rate_key,
            dimensions=dimensions,
        )
        if quote is None or (quote.unit_rate_usd is None and not quote.tiers):
            components.append(
                _unknown(
                    rule["id"],
                    primitive,
                    term,
                    (quote.unknown_reason if quote else None)
                    or f"Missing retail rate for {rate_key}.",
                )
            )
            continue
        amount = _tiered_amount(units, quote)
        if amount is None:
            components.append(
                _unknown(rule["id"], primitive, term, f"Missing retail rate for {rate_key}.")
            )
            continue
        components.append(
            CostComponent(
                rule_id=rule["id"],
                primitive_kind=primitive,
                term=term,
                amount_usd=amount,
                rate_key=rate_key,
                known=True,
            )
        )
    return components


def estimate_deployment_cost(
    inventory: PricingInventory, resolver: RetailRateResolver
) -> DeploymentCostQuote:
    """Estimate USD retail cost without evaluating pricing-rule formula strings."""

    components = [
        component
        for scenario in inventory.scenarios
        for component in _evaluate_scenario(inventory, scenario, resolver)
    ]
    known_floor = sum((component.amount_usd or Decimal(0) for component in components), Decimal(0))
    complete = all(component.known for component in components)
    if inventory.monthly_budget_usd is None:
        verdict = BudgetVerdict.NOT_PROVIDED
    elif known_floor > inventory.monthly_budget_usd:
        verdict = BudgetVerdict.EXCEEDS
    elif complete:
        verdict = BudgetVerdict.WITHIN
    else:
        verdict = BudgetVerdict.INDETERMINATE
    return DeploymentCostQuote(
        components=components,
        known_floor_usd=known_floor,
        complete=complete,
        monthly_budget_usd=inventory.monthly_budget_usd,
        verdict=verdict,
    )


__all__ = [
    "HOURS_PER_MONTH",
    "SUPPORTED_CALCULATION_KINDS",
    "BudgetVerdict",
    "CostComponent",
    "DeploymentCostQuote",
    "PricingInventory",
    "RateQuote",
    "RateTier",
    "RetailRateResolver",
    "UsageScenario",
    "estimate_deployment_cost",
]
