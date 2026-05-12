"""SOPS + age helpers for encrypting/decrypting secret files at session boundaries.

The plugin reads `.sops.yaml` at the root of HERMES_HOME (after pulling from
git) to learn which path_regex patterns should be encrypted. On session_start
it decrypts matching files in place; on session_end it re-encrypts before
commit.

v0 status: empty placeholder. Will be implemented after the scaffold is
proven to load cleanly into Hermes.
"""

# Sketch only — real implementation goes here in a follow-up commit.
#
# import re
# import subprocess
# from pathlib import Path
# import yaml
#
# def load_creation_rules(hermes_home: Path) -> list[dict]:
#     sops_yaml = hermes_home / ".sops.yaml"
#     if not sops_yaml.is_file():
#         return []
#     with sops_yaml.open() as f:
#         data = yaml.safe_load(f) or {}
#     return data.get("creation_rules", [])
#
# def decrypt_in_place(path: Path) -> None:
#     plaintext = subprocess.run(
#         ["sops", "--decrypt", str(path)],
#         capture_output=True, text=True, check=True,
#     ).stdout
#     path.write_text(plaintext)
#
# def encrypt_in_place(path: Path) -> None:
#     subprocess.run(["sops", "--encrypt", "--in-place", str(path)], check=True)
