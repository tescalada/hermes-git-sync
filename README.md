# hermes-git-sync

Two-way git sync of `HERMES_HOME` for [Hermes Agent](https://github.com/NousResearch/hermes-agent), with optional SOPS-encrypted secrets.

## What it does

Lets you manage your Hermes config (SOUL.md, USER.md, MEMORY.md, skills, etc.) the way you manage any other code — `git pull`, edit on your laptop, `git push`. The plugin keeps a running Hermes instance in sync with a git remote both directions:

- At the start of each Hermes session it `git fetch`es and rebases the agent's branch on top of `main`, so your latest hand-edits land in the volume before Hermes reads them.
- At the end of each session it commits anything the agent wrote (auto-skills, memory edits), encrypts files that match `.sops.yaml` rules, and pushes to the agent's own branch.

Conflicts only surface when both you and the agent edit the same lines of the same file; otherwise rebases auto-resolve.

## How it works

```
main                ← you edit on your laptop and push here
hermes/<machine>    ← Hermes pushes here, reads from here on session start
```

The agent only ever writes to its own branch. Your edits live on `main` and flow into the agent on the next session_start via `git rebase origin/main`. Single-writer-per-branch means the agent's pushes always succeed; there's no rebase-and-retry dance at runtime.

| When | What |
|---|---|
| `on_session_start` | `git fetch`, `git rebase origin/main` onto agent-branch, `git reset --hard`, SOPS-decrypt any encrypted files. If rebase conflicts: abort, log, agent stays on previous state — you resolve on your laptop. |
| `on_session_end` | SOPS-encrypt anything matching `.sops.yaml` creation_rules. `git commit -A` if dirty, `git push origin HEAD:<branch>`. |

Encrypted secrets are managed with [SOPS](https://github.com/getsops/sops) + [age](https://github.com/FiloSottile/age). The age private key is supplied via `SOPS_AGE_KEY_FILE` and lives **outside** `HERMES_HOME` (Docker secret or external mount).

## Installation

### Prerequisites

The plugin shells out to system binaries. The Hermes runtime needs:

- `git` (already in the upstream Hermes Docker image)
- `sops` (you must add it — see below)
- `age` (you must add it)

For the upstream `nousresearch/hermes-agent` Docker image, add a small downstream Dockerfile:

```dockerfile
FROM nousresearch/hermes-agent:latest
RUN apt-get update && apt-get install -y --no-install-recommends sops age \
    && rm -rf /var/lib/apt/lists/*
```

### Install the plugin

```sh
pip install hermes-git-sync
```

Or from a clone:

```sh
git clone https://github.com/tescalada/hermes-git-sync.git
cd hermes-git-sync
pip install -e .
```

Hermes auto-discovers it via the `hermes_agent.plugins` entry-point group on next startup.

## Configuration

Set these in your Hermes `.env` (or pass via container env vars):

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `HERMES_SYNC_REMOTE` | yes | — | Git remote URL for the state repo |
| `HERMES_SYNC_BRANCH` | no | `hermes/main` | Branch this instance owns |
| `HERMES_SYNC_SSH_KEY` | yes | — | Path to SSH deploy key (inside container) |
| `SOPS_AGE_KEY_FILE` | only if encrypting | — | Path to age private key (inside container) |

### State repo setup

1. Create a private git repo on your preferred git host. Self-hosting keeps your state inside your own network.
2. Generate an SSH deploy key for it with write access:
   ```sh
   ssh-keygen -t ed25519 -f ~/.ssh/hermes_deploy -N ""
   ```
   Add the public key (`hermes_deploy.pub`) to the repo's deploy keys list.
3. Initialize the repo with a `main` branch and the plugin's `.gitignore` and (if using encryption) `.sops.yaml` (see below).

### Encryption setup (optional but recommended)

The plugin treats every file as plaintext unless `.sops.yaml` says otherwise. To encrypt secrets:

1. Generate an age keypair on each machine that will run Hermes:
   ```sh
   age-keygen -o ~/.config/sops/age/keys.txt
   ```
   Take note of the public key (starts with `age1...`).
2. Add a `.sops.yaml` at the root of the state repo:
   ```yaml
   creation_rules:
     - path_regex: ^\.env(\.[^/]+)?$
       age: age1xxx...,age1yyy...
     - path_regex: ^auth\.json$
       age: age1xxx...,age1yyy...
   ```
   Each `age:` line is a comma-separated list of recipient public keys (one per machine).
3. Mount each machine's age private key into the Hermes container and set `SOPS_AGE_KEY_FILE` to its path.

### `.gitignore` for the state repo

Recommended deny-by-default policy so new file types Hermes invents don't accidentally leak:

```gitignore
# Deny everything by default
*
!.gitignore
!.sops.yaml

# Persona / identity / agent-curated text
!SOUL.md
!USER.md
!MEMORY.md
!memories/
!memories/**

# Auto-generated skills
!skills/
!skills/**

# UI / personality customization
!skins/
!skins/**

# Non-secret structured config
!config.yaml

# Belt-and-suspenders deny of secret-shaped paths
**/.env
**/*.key
**/*.pem
**/*.token
**/secrets*
```

`.env` and `auth.json` are reintroduced via SOPS as encrypted (`.env`, `auth.json` — encrypted in place by SOPS, the ciphertext is fine to commit since `.sops.yaml`-matched files are decrypted automatically).

## License

MIT
