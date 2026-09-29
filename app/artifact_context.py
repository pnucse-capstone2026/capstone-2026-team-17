"""Stage-agnostic selection over the current artifact trace.

The language model supplies search terms, while this module expands only refs
that already exist in the catalog and are connected by the accepted trace.
It does not decide edit authority or execute a revision; those responsibilities
remain with the stage-specific revision planner and delivery adapters.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from app.artifact_trace import ArtifactTrace, TraceRef


@dataclass(frozen=True, slots=True)
class ArtifactContextNode:
    """A catalog ref and the trace ref that grounds it."""

    ref: str
    trace_ref: TraceRef | None
    selectable: bool = True


@dataclass(frozen=True, slots=True)
class ArtifactContextRelations:
    """Catalog refs connected to one current artifact."""

    direct_upstream: tuple[str, ...] = ()
    direct_downstream: tuple[str, ...] = ()
    upstream: tuple[str, ...] = ()
    downstream: tuple[str, ...] = ()

    def public(self) -> dict[str, list[str]]:
        return {
            "direct_upstream": list(self.direct_upstream),
            "direct_downstream": list(self.direct_downstream),
            "upstream": list(self.upstream),
            "downstream": list(self.downstream),
        }


@dataclass(frozen=True, slots=True)
class ArtifactContextSelection:
    """Bounded refs for target selection and read-only evidence."""

    candidate_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    relations: Mapping[str, ArtifactContextRelations]


class ArtifactContextIndex:
    """Resolve a catalog through one accepted, cross-stage artifact trace."""

    def __init__(
        self,
        nodes: Iterable[ArtifactContextNode],
        trace: ArtifactTrace,
        *,
        aliases: Mapping[str, str] | None = None,
    ) -> None:
        self._nodes = {node.ref: node for node in nodes}
        self._aliases = {
            alias: canonical
            for alias, canonical in (aliases or {}).items()
            if canonical in self._nodes
        }
        self._trace = trace
        self._catalog_refs_by_trace: dict[TraceRef, set[str]] = {}
        for node in self._nodes.values():
            if node.trace_ref is not None:
                self._catalog_refs_by_trace.setdefault(node.trace_ref, set()).add(node.ref)
        for alias, canonical in self._aliases.items():
            try:
                trace_ref = TraceRef.parse(alias)
            except (TypeError, ValueError):
                continue
            self._catalog_refs_by_trace.setdefault(trace_ref, set()).add(canonical)

    @property
    def trace(self) -> ArtifactTrace:
        return self._trace

    def resolve(self, ref: str) -> str | None:
        if ref in self._nodes:
            return ref
        return self._aliases.get(ref)

    def relations(self, ref: str) -> ArtifactContextRelations:
        canonical = self.resolve(ref)
        node = self._nodes.get(canonical or "")
        trace_ref = node.trace_ref if node is not None else None
        if trace_ref is None or trace_ref not in self._trace.refs:
            return ArtifactContextRelations()
        return ArtifactContextRelations(
            direct_upstream=self._catalog_refs(self._trace.sources(trace_ref)),
            direct_downstream=self._catalog_refs(self._trace.consumers(trace_ref)),
            upstream=self._catalog_refs(self._trace.upstream(trace_ref)),
            downstream=self._catalog_refs(self._trace.downstream(trace_ref)),
        )

    def select(
        self,
        matched_refs: Sequence[str],
        *,
        anchor_refs: Sequence[str] = (),
        preferred_refs: Sequence[str] = (),
        candidate_scope: Iterable[str] | None = None,
        max_candidates: int = 20,
        max_evidence: int = 40,
    ) -> ArtifactContextSelection:
        """Expand search hits through exact trace edges without inventing refs."""

        anchors = self._resolved_unique(anchor_refs)
        matches = self._resolved_unique(matched_refs)
        preferred = self._resolved_unique(preferred_refs)
        roots = self._unique([*anchors, *matches])
        relations = {ref: self.relations(ref) for ref in roots}
        direct = self._unique(
            ref
            for root in roots
            for ref in (
                *relations[root].direct_upstream,
                *relations[root].direct_downstream,
            )
        )
        transitive = self._unique(
            ref
            for root in roots
            for ref in (*relations[root].upstream, *relations[root].downstream)
        )
        scope = (
            frozenset(self._resolved_unique(candidate_scope))
            if candidate_scope is not None
            else None
        )

        def candidate(ref: str) -> bool:
            node = self._nodes.get(ref)
            return bool(
                node
                and node.selectable
                and (scope is None or ref in scope)
            )

        # Anchors keep their rank, but a selected stage filters every candidate
        # so an exact same-name hit from another artifact cannot leak through.
        # A stage scope permits transitive expansion within its allowed owners;
        # an unscoped request stays at direct neighbours.
        pool = [*anchors, *preferred, *matches, *direct]
        if scope is not None:
            pool.extend(transitive)
        candidates = tuple(
            ref for ref in self._unique(pool) if candidate(ref)
        )[:max_candidates]
        evidence = tuple(
            self._unique([*matches, *anchors, *direct, *preferred, *candidates, *transitive])
        )[:max_evidence]
        return ArtifactContextSelection(
            candidate_refs=candidates,
            evidence_refs=evidence,
            relations=relations,
        )

    def _catalog_refs(self, refs: Sequence[TraceRef]) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    catalog_ref
                    for trace_ref in refs
                    for catalog_ref in self._catalog_refs_by_trace.get(trace_ref, ())
                }
            )
        )

    def _resolved_unique(self, refs: Iterable[str]) -> list[str]:
        return self._unique(
            canonical
            for ref in refs
            if isinstance(ref, str) and ref.strip()
            if (canonical := self.resolve(ref.strip())) is not None
        )

    @staticmethod
    def _unique(refs: Iterable[str]) -> list[str]:
        return list(dict.fromkeys(refs))


__all__ = [
    "ArtifactContextIndex",
    "ArtifactContextNode",
    "ArtifactContextRelations",
    "ArtifactContextSelection",
]
