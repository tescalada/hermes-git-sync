"""Session-boundary sync logic.

The plugin registers session-boundary hooks that keep `HERMES_HOME` in two-way
sync with a git remote. On session start the agent pulls user-side edits via
`git fetch` + `git rebase origin/main` onto its own branch. On session end any
agent-side writes are encrypted (per `.sops.yaml`), unchanged matched files
are restored from HEAD (so plaintext from the start-of-session decrypt never
gets staged), and the result is committed and pushed.

All callbacks accept `**kwargs` for forward-compatibility with Hermes hook
signature evolution. Errors at every step are logged at WARNING via the
package logger and swallowed — agent functionality outranks sync completeness.
"""

import logging
import os
import time
from pathlib import Path

from . import git_ops, sops_ops

logger = logging.getLogger(__name__)

# Relative path within HERMES_HOME. Lives inside `.git/` so it never gets
# committed and is naturally scoped to the local checkout.
SESSION_SENTINEL = ".git/sync-session-start"


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _branch() -> str:
    return os.environ.get("HERMES_SYNC_BRANCH", "hermes/main")


def _write_session_sentinel(home: Path) -> None:
    """Touch the sentinel; its mtime becomes the session-modified threshold."""
    sentinel = home / SESSION_SENTINEL
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()


def _session_sentinel_mtime(home: Path) -> float | None:
    """Return the sentinel's mtime, or None if it doesn't exist.

    A missing sentinel is an abort signal for `on_session_end`: there is no
    safe session-modified threshold, and any plaintext on disk from a
    successful prior decrypt step would otherwise leak via `commit_all`.
    """
    sentinel = home / SESSION_SENTINEL
    try:
        return sentinel.stat().st_mtime
    except OSError:
        return None


def _restore_unmodified_secrets(home: Path, since: float) -> tuple[int, int]:
    """Reconcile matched files older than `since` against HEAD.

    Runs after `encrypt_dirty_secrets` in `on_session_end`. Two distinct cases
    need handling here because a "matched but old-mtime" file can be either:

    1. **Tracked in HEAD as ciphertext, decrypted to plaintext by
       `on_session_start`, and untouched during the session.** `git add -A`
       in `commit_all` would otherwise stage the plaintext diff against
       HEAD's ciphertext and publish the secret on push. Restore from HEAD
       to overwrite the plaintext with the committed ciphertext.

    2. **Not in HEAD at all** — a previously-created plaintext file that
       matches `.sops.yaml` rules but was never committed (e.g. a new
       secret the user dropped in between sessions, or a Hermes-written
       file from before the plugin was active). The restore-from-HEAD
       path would fail forever. Encrypt the current plaintext in place so
       `commit_all` adds it as ciphertext.

    Encrypt updates the mtime of files it touches to "now" (> `since`), so
    the restore pass naturally skips files already handled by
    `encrypt_dirty_secrets`.

    Returns `(succeeded, failed)`. Callers MUST abort `commit_all` when
    `failed > 0`: a failure leaves plaintext in the working tree that
    `git add -A` would otherwise stage and push.
    """
    rules = sops_ops.load_creation_rules(home)
    succeeded = 0
    failed = 0
    for path in sops_ops.matching_paths(home, rules):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= since:
            continue
        try:
            rel = path.relative_to(home).as_posix()
        except ValueError:
            continue

        if git_ops.path_in_head(home, rel):
            # Case 1: tracked → restore ciphertext from HEAD.
            if git_ops.checkout_path_from_head(home, rel):
                succeeded += 1
            else:
                logger.warning("restore failed for %s", path)
                failed += 1
            continue

        # Case 2: not in HEAD. If already ciphertext on disk, leave alone;
        # commit_all will pick it up as a new file. If plaintext, encrypt
        # in place so the new file commits as ciphertext.
        if sops_ops.is_encrypted(path):
            succeeded += 1
            continue
        try:
            sops_ops.encrypt_in_place(path, home)
            succeeded += 1
        except Exception as e:
            logger.warning(
                "encrypt of new untracked secret failed for %s: %s",
                path, sops_ops._sops_error(e),
            )
            failed += 1
    return succeeded, failed


def on_session_start(
    session_id: str,
    model: str,
    platform: str,
    **kwargs,
) -> None:
    """Pull user edits into the agent's branch and decrypt covered secrets."""
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

    decrypted = 0
    decrypt_failed = 0
    try:
        decrypted, decrypt_failed = sops_ops.decrypt_known_secrets(home)
    except Exception:
        logger.warning("on_session_start: decrypt failed", exc_info=True)

    try:
        _write_session_sentinel(home)
    except Exception:
        logger.warning("on_session_start: sentinel write failed", exc_info=True)

    logger.info(
        "on_session_start: session=%s branch=%s fetched=%s rebased=%s "
        "decrypted=%d decrypt_failed=%d",
        session_id,
        branch,
        fetched,
        rebased,
        decrypted,
        decrypt_failed,
    )


def on_session_end(
    session_id: str,
    completed: bool,
    interrupted: bool,
    model: str,
    platform: str,
    **kwargs,
) -> None:
    """Encrypt session-modified secrets, restore unchanged ones, commit, push."""
    home = _hermes_home()
    branch = _branch()

    if not (home / ".git").is_dir():
        logger.warning("on_session_end: %s is not a git repo; skipping", home)
        return

    since = _session_sentinel_mtime(home)
    if since is None:
        logger.warning(
            "on_session_end: session sentinel missing; aborting before commit "
            "to avoid plaintext leak"
        )
        return

    # Pass 1: encrypt files modified during the session.
    encrypt_ok = 0
    encrypt_failed = 0
    try:
        encrypt_ok, encrypt_failed = sops_ops.encrypt_dirty_secrets(home, since=since)
    except Exception:
        logger.warning("on_session_end: encrypt raised", exc_info=True)
        encrypt_failed = 1

    if encrypt_failed > 0:
        logger.warning(
            "on_session_end: %d encrypt failure(s); aborting commit to avoid plaintext leak",
            encrypt_failed,
        )
        return

    # Pass 2: restore unchanged matched files from HEAD so the encrypted
    # version replaces the plaintext sitting in the working tree from the
    # start-of-session decrypt. Without this, `git add -A` in commit_all
    # would stage the plaintext diff against HEAD's ciphertext and publish
    # the secret on push.
    restored_ok = 0
    restored_failed = 0
    try:
        restored_ok, restored_failed = _restore_unmodified_secrets(home, since=since)
    except Exception:
        logger.warning("on_session_end: restore raised", exc_info=True)
        restored_failed = 1

    if restored_failed > 0:
        logger.warning(
            "on_session_end: %d restore failure(s); aborting commit to avoid plaintext leak",
            restored_failed,
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
        "on_session_end: session=%s branch=%s encrypted=%d restored=%d committed=%s pushed=%s",
        session_id,
        branch,
        encrypt_ok,
        restored_ok,
        committed,
        pushed,
    )
