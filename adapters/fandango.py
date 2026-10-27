"""Compatibility alias: the code moved to sources/fandango.py. The retired
sentry.py imports adapters.fandango and is kept untouched, so this
module re-points itself at the real one (same object, not a copy)."""

import sys

from sources import fandango as _impl

sys.modules[__name__] = _impl
