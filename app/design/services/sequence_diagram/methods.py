"""Compatibility exports for the public sequence syntax contract."""

from app.design.contracts.sequence import (
    is_complete_method_call,
    is_return_value_label,
    method_call_signature,
    method_name,
    method_return_type,
    normalize_return_type,
)

__all__ = [
    "is_complete_method_call",
    "is_return_value_label",
    "method_call_signature",
    "method_name",
    "method_return_type",
    "normalize_return_type",
]
