"""SOPS + age helpers for encrypting and decrypting secret files.

Reads `.sops.yaml` at the root of HERMES_HOME to learn which `path_regex`
patterns should be encrypted. On session start, decrypts matching encrypted
files in place. On session end, encrypts matching plaintext files that were
modified during the session.
"""

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# A SOPS-encrypted file always carries three pieces of metadata: a version, an
# integrity MAC, and at least one recipient block. Detection requires all
# three — a single-marker check would misclassify plaintext config files that
# legitimately contain `sops:` or `sops_version=`.
_RECIPIENT_KEYS = ("kms", "age", "gcp_kms", "azure_kv", "hc_vault", "pgp")

# Hard cap for files we'll fully read into memory for encryption detection.
# SOPS-encrypted secret files are typically <100 KiB; 5 MiB is generous and
# bounds memory use on adversarial inputs.
_DETECT_MAX_BYTES = 5 * 1024 * 1024

# Env-format markers must appear at line start. A plaintext `.env` containing
# a comment like `# sops_version=...` would otherwise false-positive.
_ENV_VERSION_PAT = re.compile(r"^sops_version=", re.MULTILINE)
_ENV_MAC_PAT = re.compile(r"^sops_mac=", re.MULTILINE)
_ENV_RECIPIENT_PATS = tuple(re.compile(rf"^sops_{k}__", re.MULTILINE) for k in _RECIPIENT_KEYS)


def _is_encrypted_yaml_or_json_dict(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    sops = data.get("sops")
    if not isinstance(sops, dict):
        return False
    if "version" not in sops or "mac" not in sops:
        return False
    return any(sops.get(k) for k in _RECIPIENT_KEYS)


def _is_encrypted_env(content: str) -> bool:
    if not _ENV_VERSION_PAT.search(content):
        return False
    if not _ENV_MAC_PAT.search(content):
        return False
    return any(p.search(content) for p in _ENV_RECIPIENT_PATS)


def is_encrypted(path: Path) -> bool:
    """True iff `path` carries SOPS's full metadata structure.

    Detection requires (a) `sops.version` / `sops_version`, (b) `sops.mac` /
    `sops_mac`, and (c) at least one recipient block (kms/age/gcp_kms/azure_kv/
    hc_vault/pgp). Single-marker checks would falsely classify plaintext
    configs containing literal `sops:` or `sops_version=`, causing
    `encrypt_dirty_secrets` to skip them and commit plaintext.

    Files larger than `_DETECT_MAX_BYTES` (5 MiB) return False — SOPS-encrypted
    secrets don't get that large, and reading the file would blow the memory
    bound. The protective path at encrypt time (sops itself errors when asked
    to re-encrypt an encrypted file) prevents double-encryption regardless.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size > _DETECT_MAX_BYTES:
        return False
    try:
        content = path.read_text(errors="replace")
    except OSError:
        return False
    name = path.name.lower()
    if name.endswith((".yaml", ".yml")):
        try:
            return _is_encrypted_yaml_or_json_dict(yaml.safe_load(content))
        except yaml.YAMLError:
            return False
    if name.endswith(".json"):
        try:
            return _is_encrypted_yaml_or_json_dict(json.loads(content))
        except json.JSONDecodeError:
            return False
    return _is_encrypted_env(content)


def load_creation_rules(hermes_home: Path) -> list[dict]:
    """Load `creation_rules` from `.sops.yaml`. Returns [] when missing/malformed."""
    sops_yaml = hermes_home / ".sops.yaml"
    if not sops_yaml.is_file():
        return []
    try:
        with sops_yaml.open() as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        logger.warning("failed to parse %s: %s", sops_yaml, e)
        return []
    if not isinstance(data, dict):
        return []
    rules = data.get("creation_rules") or []
    return [r for r in rules if isinstance(r, dict)]


def matching_paths(hermes_home: Path, rules: list[dict]) -> list[Path]:
    """Return regular files under `hermes_home` matching any rule's `path_regex`.

    Uses `os.walk(followlinks=False)` so directory symlinks aren't traversed
    (a symlinked dir cycle would otherwise trap the walk). File symlinks are
    also skipped — `sops --in-place` on a symlink would write to its target,
    potentially outside the repo. `.git` directories at any depth are pruned.
    """
    if not rules:
        return []
    patterns: list[re.Pattern] = []
    for r in rules:
        regex = r.get("path_regex")
        if not regex:
            continue
        try:
            patterns.append(re.compile(regex))
        except re.error as e:
            logger.warning("bad path_regex in .sops.yaml: %r (%s)", regex, e)
    if not patterns:
        return []
    matches: list[Path] = []
    for root, dirs, files in os.walk(hermes_home, followlinks=False):
        dirs[:] = [d for d in dirs if d != ".git" and not (Path(root) / d).is_symlink()]
        for fname in files:
            p = Path(root) / fname
            if p.is_symlink():
                continue
            try:
                rel = p.relative_to(hermes_home).as_posix()
            except ValueError:
                continue
            # Never match the rules file itself — encrypting it would brick
            # the next session's load_creation_rules and lock the agent out.
            if rel == ".sops.yaml":
                continue
            if any(pat.search(rel) for pat in patterns):
                matches.append(p)
    return matches


# Env-var allowlist for the sops subprocess. Sops honors several env vars
# that override the loaded config — most consequentially SOPS_AGE_RECIPIENTS
# (substitutes the recipient set, defeating `--config` pinning) and the
# SOPS_{KMS,GCP,PGP,AZ_KV,VAULT}_* variants. Inheriting the parent process
# env would let any caller (or prompt-injected agent shell) override the
# pinned recipients. Allowlist limits sops to the variables it actually
# needs: PATH/HOME/locale/etc. for runtime, and SOPS_AGE_KEY{,_FILE} so the
# decrypt path can find the local age private key.
_SOPS_SUBPROCESS_ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TZ", "TERM", "LD_LIBRARY_PATH",
    "SOPS_AGE_KEY_FILE", "SOPS_AGE_KEY", "SOPS_AGE_KEY_CMD",
)


def _sops_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in _SOPS_SUBPROCESS_ENV_ALLOWLIST}


def _dump_stderr_locally(home: Path, stderr: str) -> Path | None:
    """Persist sops's stderr under `<home>/.git/sops-errors/` (mode 0600).

    Sops's stderr on encrypt/decrypt failures can include snippets of the
    offending input. Sending it through the structured logger would forward
    those plaintext snippets to centralized log sinks. Writing it to a
    restricted-perms file inside `.git/` keeps the diagnostic local to the
    checkout — operators read it with `cat`, log shippers don't see it,
    `.git/` is never committed.

    Filename uses `time.time_ns()` (nanosecond precision) so multiple
    failures within the same wall-clock second don't overwrite each other.
    Directory is created with `mode=0o700` so the listing (timestamps/pids)
    isn't world-readable; file contents are also `chmod 0600`. On OSError
    (disk full, read-only fs, etc.) the function returns None and logs a
    warning WITHOUT the stderr content — caller still has the diagnostic
    breadcrumb that the dump itself failed.
    """
    if not stderr:
        return None
    try:
        logdir = home / ".git" / "sops-errors"
        logdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # `mkdir(exist_ok=True, mode=...)` doesn't tighten an existing dir's
        # mode. Apply explicitly so a previously-created looser dir doesn't
        # leak filenames (timestamps/pids) to other local users.
        os.chmod(logdir, 0o700)
        fp = logdir / f"sops-{time.time_ns()}-{os.getpid()}.err"
        fp.write_text(stderr)
        os.chmod(fp, 0o600)
        return fp
    except OSError as e:
        # Log only the OSError shape, never the stderr content.
        logger.warning(
            "failed to dump sops stderr locally: %s: %s",
            type(e).__name__, e,
        )
        return None


def _sops(action: str, path: Path, home: Path) -> None:
    """Run `sops <action> --in-place <path>` pinned to `<home>/.sops.yaml`.

    `--config` is passed explicitly so sops uses exactly the home-root rules
    instead of walking up from its CWD (which a nested `.sops.yaml` could
    shadow). Env is scrubbed via `_SOPS_SUBPROCESS_ENV_ALLOWLIST` so env-var
    overrides (notably SOPS_AGE_RECIPIENTS) can't substitute recipients out
    from under the pinned config. `home` is resolved to absolute so a
    relative HERMES_HOME doesn't silently re-open the CWD-walk failure mode.

    On non-zero exit, stderr is dumped to a local file under `.git/` and a
    `CalledProcessError` is raised for the caller's normal abort path. The
    raised exception still carries stderr in its `.stderr` attribute, but
    callers must NOT log it — they format the failure via `_sops_error`
    which emits only the returncode.
    """
    home = home.resolve()
    config = home / ".sops.yaml"
    result = subprocess.run(
        ["sops", "--config", str(config), action, "--in-place", str(path)],
        cwd=str(home),
        env=_sops_env(),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _dump_stderr_locally(home, result.stderr)
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )


def encrypt_in_place(path: Path, home: Path) -> None:
    _sops("--encrypt", path, home)


def decrypt_in_place(path: Path, home: Path) -> None:
    _sops("--decrypt", path, home)


def encrypt_dirty_secrets(hermes_home: Path, since: float) -> tuple[int, int]:
    """Encrypt matching plaintext files modified since `since` (Unix mtime).

    Files whose own `st_mtime < since` are skipped — re-encrypting an
    unchanged plaintext file would produce fresh ciphertext (new SOPS data key)
    and pollute git history with noise commits. Strict `<` (not `<=`) avoids
    dropping files modified in the same wall-clock second as the sentinel on
    1-second-mtime-granularity filesystems.

    Returns `(succeeded, failed)`. Callers must abort the commit step when
    `failed > 0` — committing while any matched file remains plaintext would
    leak it via git history.
    """
    rules = load_creation_rules(hermes_home)
    succeeded = 0
    failed = 0
    for path in matching_paths(hermes_home, rules):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < since:
            continue
        if is_encrypted(path):
            continue
        try:
            encrypt_in_place(path, hermes_home)
            succeeded += 1
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.warning("sops encrypt failed for %s: %s", path, _sops_error(e))
            failed += 1
    return succeeded, failed


def decrypt_known_secrets(hermes_home: Path) -> tuple[int, int]:
    """Decrypt all matching files currently encrypted. Returns `(succeeded, failed)`."""
    rules = load_creation_rules(hermes_home)
    succeeded = 0
    failed = 0
    for path in matching_paths(hermes_home, rules):
        if not is_encrypted(path):
            continue
        try:
            decrypt_in_place(path, hermes_home)
            succeeded += 1
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logger.warning("sops decrypt failed for %s: %s", path, _sops_error(e))
            failed += 1
    return succeeded, failed


def _sops_error(e: Exception) -> str:
    """Compact error formatter for sops failures.

    Only `rc` is included in the log line. Sops's stderr on encrypt/decrypt
    failures can echo snippets of the input file content (e.g., YAML parse
    errors reproduce the offending line). Surfacing stderr through the
    structured logger would forward those plaintext snippets to whatever
    centralized log sink the operator ships logs to — a plaintext-leak
    channel the pre-fix code didn't have. Operators diagnose by reproducing
    the sops invocation manually with stderr visible on a local terminal.
    """
    if isinstance(e, subprocess.CalledProcessError):
        return f"rc={e.returncode}"
    return f"{type(e).__name__}: {e}"
