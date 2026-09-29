"""기존 ERD logical mapping import와 Unmapped 어휘를 보존하는 호환 facade다."""

from app.design.services.erd.inheritance import (
    UNMAPPED_INHERITANCE_CYCLE,
    UNMAPPED_MULTIPLE_INHERITANCE,
)
from app.design.services.erd.projection import build_logical_model
from app.design.services.erd.relationship_mapping import (
    STRUCTURAL_TYPES,
    UNMAPPED_DEPENDENCY,
    UNMAPPED_DUPLICATE_JUNCTION,
    UNMAPPED_DUPLICATE_RELATIONSHIP,
    UNMAPPED_MANDATORY_REFERENCE_CYCLE,
    UNMAPPED_MULTIPLICITY,
)
from app.design.services.erd.table_mapping import (
    KEY_INHERITED,
    KEY_NATURAL,
    KEY_SURROGATE,
)

__all__ = [
    "KEY_INHERITED",
    "KEY_NATURAL",
    "KEY_SURROGATE",
    "STRUCTURAL_TYPES",
    "UNMAPPED_DEPENDENCY",
    "UNMAPPED_DUPLICATE_JUNCTION",
    "UNMAPPED_DUPLICATE_RELATIONSHIP",
    "UNMAPPED_INHERITANCE_CYCLE",
    "UNMAPPED_MANDATORY_REFERENCE_CYCLE",
    "UNMAPPED_MULTIPLE_INHERITANCE",
    "UNMAPPED_MULTIPLICITY",
    "build_logical_model",
]
