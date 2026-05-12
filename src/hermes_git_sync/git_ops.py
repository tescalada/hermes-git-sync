"""Thin wrappers around the system `git` binary.

We shell out rather than depend on pygit2 / GitPython because:
  - `git` is already in the upstream Hermes Docker image
  - shelling out gives us full git CLI behavior incl. SSH keys via GIT_SSH_COMMAND
  - we don't need fancy in-process git operations

v0 status: empty placeholder. Will be implemented after the scaffold is
proven to load cleanly into Hermes.
"""

# Sketch only — real implementation goes here in a follow-up commit.
#
# import os
# import subprocess
# from pathlib import Path
#
# def run(args: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
#     ssh_key = os.environ.get("HERMES_SYNC_SSH_KEY")
#     env = os.environ.copy()
#     if ssh_key:
#         env["GIT_SSH_COMMAND"] = f"ssh -i {ssh_key} -o StrictHostKeyChecking=accept-new"
#     return subprocess.run(
#         ["git", *args],
#         cwd=str(cwd),
#         env=env,
#         capture_output=True,
#         text=True,
#         check=check,
#     )
#
# def status_dirty(cwd: Path) -> bool:
#     return bool(run(["status", "--porcelain"], cwd=cwd).stdout.strip())
