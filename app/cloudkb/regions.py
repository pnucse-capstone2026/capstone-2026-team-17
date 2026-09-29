"""Resolve user-written locations to AWS, Azure, or GCP region codes."""
from __future__ import annotations

from dataclasses import dataclass

from app.cloudkb import region_catalog as _regions


@dataclass(frozen=True)
class RegionCandidate:
    """해석 후보 하나. **원문을 함께 들고 다닌다** — 틀렸을 때 되짚을 근거다."""

    code: str
    provider: str
    display_name: str
    as_written: str


def resolve(query: str, *, provider: str | None = None) -> tuple[RegionCandidate, ...]:
    """사람이 쓴 리전 표현에서 후보를 찾는다. 못 알아들으면 빈 튜플.

    `provider`를 알면 그 안에서만 찾는다 — 프로바이더가 정해진 뒤에는 후보가 대개
    하나로 떨어진다.
    """
    if not (query or "").strip():
        return ()
    matches = _regions.resolve_region(query, provider=provider)
    return tuple(
        RegionCandidate(
            code=m.code,
            provider=m.provider,
            display_name=getattr(m, "display_name", "") or getattr(m, "name", ""),
            as_written=query,
        )
        for m in matches
    )


def providers() -> tuple[str, ...]:
    """Return supported provider identifiers present in the region catalog."""
    return _regions.providers()


def is_region_code(value: str, *, provider: str | None = None) -> bool:
    """이미 리전 **코드**인가 — 지명을 코드 자리에 넣은 것을 잡는다."""
    return any(c.code == value for c in resolve(value, provider=provider))
