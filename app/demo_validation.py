"""Shared opt-in switch for demo-only validation skipping."""

import os

DEMO_SKIP_VALIDATION_ENV = "EASYDEP_DEMO_SKIP_VALIDATION"


def demo_skip_validation_enabled() -> bool:
    """Return true only for the explicit shared demo flag value."""

    return os.getenv(DEMO_SKIP_VALIDATION_ENV, "").strip().casefold() == "true"
