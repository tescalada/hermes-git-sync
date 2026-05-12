"""Session-boundary sync logic.

v0 status: hooks defined with the upstream-documented signatures, but the
bodies are no-op placeholders that just log. Real implementations land in
follow-up commits, in this order:

  1. `_git()` wrapper and `_status_dirty()` helper       (git_ops.py)
  2. `on_session_end`: commit + push (no encryption yet) (sync.py)
  3. `on_session_start`: fetch + rebase                  (sync.py)
  4. SOPS encrypt / decrypt around commit + apply        (sops_ops.py)
  5. `.sops.yaml` rule discovery                         (sops_ops.py)

All callbacks accept `**kwargs` for forward-compatibility with Hermes hook
signature evolution.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _branch() -> str:
    return os.environ.get("HERMES_SYNC_BRANCH", "hermes/main")


def on_session_start(
    session_id: str,
    model: str,
    platform: str,
    **kwargs,
) -> None:
    """Fires on the first turn of a new Hermes session.

    Will eventually:
      1. `git fetch --all` in HERMES_HOME
      2. `git rebase origin/main` (linear, clean-merge or skip on conflict)
      3. `git reset --hard HEAD` to apply to volume
      4. SOPS-decrypt any files matching `.sops.yaml` creation_rules

    For v0, just logs that the hook fired.
    """
    logger.info(
        "on_session_start fired (session_id=%s, model=%s, platform=%s) — v0 noop",
        session_id, model, platform,
    )


def on_session_end(
    session_id: str,
    completed: bool,
    interrupted: bool,
    model: str,
    platform: str,
    **kwargs,
) -> None:
    """Fires at the end of each run_conversation call (and on CLI exit).

    Will eventually:
      1. SOPS-encrypt any plain-text files matching `.sops.yaml` creation_rules
      2. `git status` — bail if no diff
      3. `git add -A && git commit -m "<session_id> <ts>"`
      4. `git push origin HEAD:<HERMES_SYNC_BRANCH>` (always succeeds — single writer)

    For v0, just logs that the hook fired.
    """
    logger.info(
        "on_session_end fired (session_id=%s, completed=%s, interrupted=%s, model=%s, platform=%s) — v0 noop",
        session_id, completed, interrupted, model, platform,
    )
