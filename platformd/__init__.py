"""platformd — the multi-tenant Phase 3 walking skeleton.

Named platformd (platform daemon), not platform, because a package named
platform run from the repo root shadows the stdlib `platform` module for
every script in this tree (sentry.py, watchdog.py, etc. all still need the
real one). See docs/architecture-notes.md / the Phase 3 build notes for the
naming rationale.

stdlib only. Nothing under this package may import third-party code.
"""
