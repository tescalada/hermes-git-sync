"""Session-boundary sync logic.

The plugin registers session-boundary hooks that keep `HERMES_HOME` in two-way
sync with a git remote. On session start the agent pulls user-side edits via
`git fetch` + `git rebase origin/main` onto its own branch. On session end any
agent-side writes are committed and pushed.

Encryption of matched paths (`.env`, `auth.json`, etc.) is git-crypt's job,
handled transparently by the clean/smudge filters configured in the state
repo's `.gitattributes`. The plugin never sees ciphertext: working-tree files
are always plaintext, and `git add` runs the clean filter which writes
ciphertext into the index. The deployment is responsible for `git-crypt
unlock` before the first session runs.

All callbacks accept `**kwargs` for forward-compatibility with Hermes hook
signature evolution. Errors at every step are logged at WARNING via the
package logger and swallowed — agent functionality outranks sync completeness.
"""

import logging
import os
from pathlib import Path
import time

from . import git_ops

logger = logging.getLogger(__name__)


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _branch() -> str:
    return os.environ.get("HERMES_SYNC_BRANCH", "hermes/main")


def _git_crypt_filter_missing(home: Path) -> bool:
    """True if `.gitattributes` declares an active git-crypt filter rule but
    the local repo has no `filter.git-crypt.clean` config.

    When this returns True, `git add -A` would silently stage plaintext blobs
    for matched paths (a `filter=git-crypt` attribute without the corresponding
    config is treated as no filter at all). On_session_end MUST abort in that
    state to avoid pushing plaintext secrets.

    Returns False when there is no active git-crypt rule declared (vanilla
    repo, or rule is fully commented out) or when the filter IS configured
    (normal case). Reads `.gitattributes` as bytes so a non-UTF-8 file
    doesn't crash the hook; recognizes `#` line comments (whole-line
    comments only, matching git's own gitattributes parser — inline `#`
    is not a comment marker).
    """
    gitattributes = home / ".gitattributes"
    try:
        content = gitattributes.read_bytes()
    except OSError:
        return False
    declared = False
    for raw in content.splitlines():
        line = raw.lstrip()
        if not line or line.startswith(b"#"):
            continue
        if b"filter=git-crypt" in line:
            declared = True
            break
    if not declared:
        return False
    result = git_ops.run(
        ["config", "--get", "filter.git-crypt.clean"],
        cwd=home,
        check=False,
    )
    return not result.stdout.strip()


def on_session_start(
    session_id: str,
    model: str,
    platform: str,
    **kwargs,
) -> None:
    """Pull user edits into the agent's branch."""
    home = _hermes_home()
    branch = _branch()

    if not (home / ".git").is_dir():
        logger.warning("on_session_start: %s is not a git repo; skipping", home)
        return

    fetched = False
    try:
        git_ops.fetch(home)
        fetched = True
    except Exception:
        logger.warning("on_session_start: fetch failed (continuing offline)", exc_info=True)

    try:
        git_ops.ensure_branch(home, branch)
    except Exception:
        logger.warning("on_session_start: ensure_branch failed", exc_info=True)
        return

    rebased = False
    if fetched:
        try:
            rebased = git_ops.rebase(home, "origin/main")
        except Exception:
            logger.warning("on_session_start: rebase raised", exc_info=True)

    try:
        git_ops.reset_hard(home)
    except Exception:
        logger.warning("on_session_start: reset_hard failed", exc_info=True)

    logger.info(
        "on_session_start: session=%s branch=%s fetched=%s rebased=%s",
        session_id,
        branch,
        fetched,
        rebased,
    )


def on_session_end(
    session_id: str,
    completed: bool,
    interrupted: bool,
    model: str,
    platform: str,
    **kwargs,
) -> None:
    """Commit any session-side writes and push to the agent's branch."""
    home = _hermes_home()
    branch = _branch()

    if not (home / ".git").is_dir():
        logger.warning("on_session_end: %s is not a git repo; skipping", home)
        return

    if _git_crypt_filter_missing(home):
        logger.warning(
            "on_session_end: .gitattributes declares filter=git-crypt but the "
            "local repo has no filter.git-crypt.clean config; aborting before "
            "commit to avoid pushing plaintext secrets. Run `git-crypt unlock` "
            "and retry."
        )
        return

    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    message = f"{session_id} {ts}"

    committed = False
    try:
        committed = git_ops.commit_all(home, message)
    except Exception:
        logger.warning("on_session_end: commit failed", exc_info=True)

    pushed = False
    if committed:
        try:
            git_ops.push(home, branch)
            pushed = True
        except Exception:
            logger.warning("on_session_end: push failed", exc_info=True)

    logger.info(
        "on_session_end: session=%s branch=%s committed=%s pushed=%s",
        session_id,
        branch,
        committed,
        pushed,
    )
