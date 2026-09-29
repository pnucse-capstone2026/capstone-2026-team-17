"""Offline, versioned retail-rate snapshots and deterministic resolvers.

Snapshots are the source of truth for non-VM prices.  The bundled CostKB VM
catalogue is deliberately only a narrow fallback for whole-instance AWS and
Azure runtime prices; it cannot safely derive GCP's separate vCPU/memory rates.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .dataset import dataset_note, load_specs
from .deployment_cost import RateQuote, RetailRateResolver

SNAPSHOT_SCHEMA_VERSION = "easydep-retail-rate-snapshot/v1"


def _normalise_text(value: Any) -> str:
    return str(value).strip()


def normalize_dimensions(dimensions: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """Make matching insensitive to mapping order, but never fuzzy-match values."""

    return tuple(
        sorted((_normalise_text(key), _normalise_text(value)) for key, value in dimensions.items())
    )


class SnapshotRate(BaseModel):
    """One exact retail meter from a reviewed source snapshot."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    provider: Literal["aws", "azure", "gcp"]
    region: str = Field(min_length=1)
    rule_id: str = Field(alias="ruleId", min_length=1)
    rate_key: str = Field(alias="rateKey", min_length=1)
    dimensions: dict[str, str] = Field(default_factory=dict)
    unit_rate_usd: Decimal = Field(alias="unitRateUsd", gt=0)
    units_per_rate: Decimal = Field(default=Decimal(1), alias="unitsPerRate", gt=0)
    source_ref: str | None = Field(default=None, alias="sourceRef", min_length=1)
    effective_at: datetime | None = Field(default=None, alias="effectiveAt")

    @model_validator(mode="after")
    def _normalise_identity(self) -> SnapshotRate:
        self.region = _normalise_text(self.region).lower()
        self.rule_id = _normalise_text(self.rule_id)
        self.rate_key = _normalise_text(self.rate_key)
        self.dimensions = {key: value for key, value in normalize_dimensions(self.dimensions)}
        return self

    @property
    def key(self) -> tuple[str, str, str, str, tuple[tuple[str, str], ...]]:
        return (
            self.provider,
            self.region,
            self.rule_id,
            self.rate_key,
            normalize_dimensions(self.dimensions),
        )


class RetailRateSnapshot(BaseModel):
    """Typed JSON envelope whose digest is stable across mapping/list order."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    schema_version: Literal[SNAPSHOT_SCHEMA_VERSION] = Field(alias="schemaVersion")
    source_ref: str = Field(alias="sourceRef", min_length=1)
    effective_at: datetime = Field(alias="effectiveAt")
    stale_after: datetime | None = Field(default=None, alias="staleAfter")
    rates: list[SnapshotRate] = Field(min_length=1)

    @model_validator(mode="after")
    def _require_unique_exact_keys(self) -> RetailRateSnapshot:
        keys = [rate.key for rate in self.rates]
        if len(keys) != len(set(keys)):
            raise ValueError("Snapshot contains duplicate exact retail-rate keys.")
        return self

    @property
    def snapshot_digest(self) -> str:
        body = self.model_dump(mode="json", by_alias=True)
        body["rates"] = sorted(
            body["rates"],
            key=lambda rate: json.dumps(rate, sort_keys=True, separators=(",", ":")),
        )
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def is_stale(self, now: datetime | None = None) -> bool:
        if self.stale_after is None:
            return False
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        stale_after = self.stale_after
        if stale_after.tzinfo is None:
            stale_after = stale_after.replace(tzinfo=UTC)
        return current > stale_after


def load_retail_rate_snapshot(
    payload: str | bytes | bytearray | Mapping[str, Any],
) -> RetailRateSnapshot:
    """Validate a JSON document or equivalent mapping before it can price anything."""

    if isinstance(payload, (str, bytes, bytearray)):
        return RetailRateSnapshot.model_validate_json(payload)
    return RetailRateSnapshot.model_validate(payload)


class SnapshotRateResolver:
    """Resolve only an exact, normalised snapshot key; no region/SKU fallbacks."""

    def __init__(self, snapshot: RetailRateSnapshot, *, now: datetime | None = None) -> None:
        self.snapshot = snapshot
        self._now = now
        self._rates = {rate.key: rate for rate in snapshot.rates}

    def resolve_rate(
        self,
        *,
        provider: str,
        rule_id: str,
        rate_key: str,
        dimensions: Mapping[str, Any],
    ) -> RateQuote | None:
        if any(
            value is None or (isinstance(value, str) and not value.strip())
            for value in dimensions.values()
        ):
            return None
        region = dimensions.get("region") or dimensions.get("armRegionName") or dimensions.get(
            "serviceRegions"
        )
        if region is None:
            return None
        key = (
            _normalise_text(provider).lower(),
            _normalise_text(region).lower(),
            _normalise_text(rule_id),
            _normalise_text(rate_key),
            normalize_dimensions(dimensions),
        )
        rate = self._rates.get(key)
        if rate is None:
            return None
        return RateQuote(
            rate_key=rate_key,
            unit_rate_usd=rate.unit_rate_usd,
            units_per_rate=rate.units_per_rate,
            source=rate.source_ref or self.snapshot.source_ref,
            source_ref=rate.source_ref or self.snapshot.source_ref,
            effective_at=rate.effective_at or self.snapshot.effective_at,
            snapshot_digest=self.snapshot.snapshot_digest,
            stale=self.snapshot.is_stale(self._now),
        )


_VM_RUNTIME_KEYS = {
    ("aws", "aws.compute.ec2-on-demand", "instance_runtime_rate"),
    ("azure", "azure.compute.linux-vm-payg", "vm_runtime_rate"),
}


class VmDatasetRateResolver:
    """Exact whole-VM hourly rates from CostKB's already-reviewed VM catalogue."""

    def __init__(
        self,
        specs: Iterable[Mapping[str, Any]] | None = None,
        *,
        output_dir: Path | str | None = None,
    ) -> None:
        rows = list(load_specs(output_dir) if specs is None else specs)
        self._source_ref = "costkb-vm-dataset"
        self._source_note = dataset_note(output_dir) if specs is None else "supplied-costkb-vm-dataset"
        self._index: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
        for row in rows:
            provider, region, sku = (row.get(name) for name in ("provider", "region", "specName"))
            if not all(isinstance(value, str) and value.strip() for value in (provider, region, sku)):
                continue
            key = (provider.strip().lower(), region.strip().lower(), sku.strip().lower())
            self._index.setdefault(key, []).append(row)

    def resolve_rate(
        self,
        *,
        provider: str,
        rule_id: str,
        rate_key: str,
        dimensions: Mapping[str, Any],
    ) -> RateQuote | None:
        normalised_provider = _normalise_text(provider).lower()
        if (normalised_provider, rule_id, rate_key) not in _VM_RUNTIME_KEYS:
            return None
        region = dimensions.get("region") or dimensions.get("armRegionName")
        sku = (
            dimensions.get("instance_type")
            or dimensions.get("vm_size")
            or dimensions.get("armSkuName")
            or dimensions.get("sku")
        )
        if not isinstance(region, str) or not isinstance(sku, str):
            return None
        rows = self._index.get(
            (normalised_provider, region.strip().lower(), sku.strip().lower()), []
        )
        if len(rows) != 1:
            return None
        hourly = rows[0].get("hourlyUSD")
        if hourly is None:
            return None
        try:
            rate = Decimal(str(hourly))
        except Exception:
            return None
        if rate <= 0:
            return None
        return RateQuote(
            rate_key=rate_key,
            unit_rate_usd=rate,
            source=self._source_ref,
            source_ref=self._source_ref,
            unknown_reason=None,
        )


class CompositeRetailRateResolver:
    """Use reviewed snapshot meters first, then the narrow VM catalogue fallback."""

    def __init__(self, *resolvers: RetailRateResolver) -> None:
        self._resolvers = resolvers

    def resolve_rate(self, **kwargs: Any) -> RateQuote | None:
        for resolver in self._resolvers:
            quote = resolver.resolve_rate(**kwargs)
            if quote is not None:
                return quote
        return None


def build_retail_rate_resolver(
    snapshot: RetailRateSnapshot,
    *,
    vm_specs: Iterable[Mapping[str, Any]] | None = None,
    output_dir: Path | str | None = None,
    now: datetime | None = None,
) -> CompositeRetailRateResolver:
    """Build the offline composition without fetching prices or credentials."""

    return CompositeRetailRateResolver(
        SnapshotRateResolver(snapshot, now=now),
        VmDatasetRateResolver(vm_specs, output_dir=output_dir),
    )


__all__ = [
    "SNAPSHOT_SCHEMA_VERSION",
    "CompositeRetailRateResolver",
    "RetailRateSnapshot",
    "SnapshotRate",
    "SnapshotRateResolver",
    "ValidationError",
    "VmDatasetRateResolver",
    "build_retail_rate_resolver",
    "load_retail_rate_snapshot",
    "normalize_dimensions",
]
