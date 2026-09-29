"""`python -m costkb` 진입점."""

from __future__ import annotations

from app.cloudkb.costkb.cli import main
from app.cloudkb.kbcommon.console import use_utf8

use_utf8()
raise SystemExit(main())
