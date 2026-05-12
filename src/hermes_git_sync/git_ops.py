"""Thin wrappers around the system `git` binary.

We shell out rather than depend on pygit2 / GitPython because:
  - `git` is already in the upstream Hermes Docker image
  - shelling out gives us full git CLI behavior incl. SSH keys via GIT_SSH_COMMAND
  - we don't need fancy in-process git operations
"""

import logging
import os
import shlex
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Default committer identity for commits made inside the agent container.
# Without these, `git commit` fails with "Please tell me who you are" and the
# surrounding error handler in sync.py swallows the failure as a silent no-op.
_COMMIT_IDENTITY = [
    "-c", "user.name=hermes-git-sync",
    "-c", "user.email=hermes-git-sync@localhost",
]


# Allowlist for the git subprocess environment. Starting from a minimal set
# (rather than copying the full process env) keeps secrets like
# `SOPS_AGE_KEY_FILE` out of git's invocations — they'd otherwise be visible
# to commit hooks, credential helpers, and any tool git execs.
_GIT_ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TZ", "TERM")
_GIT_ENV_PREFIXES = ("GIT_", "SSH_", "GPG_")


def _env() -> dict[str, str]:
    """Build env for git, configuring SSH via GIT_SSH_COMMAND when a key is set."""
    env = {
        k: v for k, v in os.environ.items()
        if k in _GIT_ENV_ALLOWLIST
        or any(k.startswith(p) for p in _GIT_ENV_PREFIXES)
    }
    ssh_key = os.environ.get("HERMES_SYNC_SSH_KEY")
    if ssh_key:
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {shlex.quote(ssh_key)} -o IdentitiesOnly=yes "
            f"-o StrictHostKeyChecking=accept-new"
        )
    return env


def run(
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run git with the given args. Captures stdout and stderr as text."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=_env(),
        capture_output=True,
        text=True,
        check=check,
    )


def status_dirty(cwd: Path) -> bool:
    """True if the working tree or index differs from HEAD."""
    return bool(run(["status", "--porcelain"], cwd=cwd).stdout.strip())


def current_branch(cwd: Path) -> str:
    return run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd).stdout.strip()


def ref_exists(cwd: Path, ref: str) -> bool:
    return run(
        ["rev-parse", "--verify", "--quiet", ref],
        cwd=cwd,
        check=False,
    ).returncode == 0


def ensure_branch(cwd: Path, branch: str) -> None:
    """Ensure HEAD is on `branch`.

    Resolution order: already on it → local ref exists → remote `origin/<branch>`
    exists → fall back to creating from `origin/main`.
    """
    if current_branch(cwd) == branch:
        return
    if ref_exists(cwd, branch):
        run(["checkout", branch], cwd=cwd)
        return
    if ref_exists(cwd, f"origin/{branch}"):
        run(["checkout", "-b", branch, f"origin/{branch}"], cwd=cwd)
        return
    run(["checkout", "-b", branch, "origin/main"], cwd=cwd)


def fetch(cwd: Path) -> None:
    run(["fetch", "--all", "--prune"], cwd=cwd)


def rebase(cwd: Path, upstream: str) -> bool:
    """Rebase the current branch onto `upstream`.

    Returns True on clean rebase. On any conflict, aborts and returns False so
    the agent stays on its previous state instead of getting jammed.
    """
    result = run(["rebase", upstream], cwd=cwd, check=False)
    if result.returncode == 0:
        return True
    run(["rebase", "--abort"], cwd=cwd, check=False)
    logger.warning(
        "rebase onto %s failed; aborted (rc=%d)",
        upstream, result.returncode,
    )
    return False


def reset_hard(cwd: Path) -> None:
    """Reset working tree and index to HEAD."""
    run(["reset", "--hard", "HEAD"], cwd=cwd)


def checkout_path_from_head(cwd: Path, rel_path: str) -> bool:
    """Restore one path in the working tree from HEAD.

    Used by the restore pass in `on_session_end` to bring an unchanged-during-
    session secret back to its committed (encrypted) form before `commit_all`
    runs `git add -A`. Returns True on success; False if the path isn't in
    HEAD or git refuses for another reason (caller treats as a failure).
    """
    result = run(["checkout", "HEAD", "--", rel_path], cwd=cwd, check=False)
    return result.returncode == 0


def commit_all(cwd: Path, message: str) -> bool:
    """Stage everything and commit. Returns True iff a commit was created."""
    run(["add", "-A"], cwd=cwd)
    if not status_dirty(cwd):
        return False
    run([*_COMMIT_IDENTITY, "commit", "-m", message], cwd=cwd)
    return True


def push(cwd: Path, branch: str) -> None:
    """Push HEAD to `origin/<branch>`. Single-writer guarantees this succeeds."""
    run(["push", "origin", f"HEAD:{branch}"], cwd=cwd)
