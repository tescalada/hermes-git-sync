# hermes-git-sync

Two-way git sync of `HERMES_HOME` for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

## What it does

Lets you manage your Hermes config (SOUL.md, USER.md, MEMORY.md, skills, etc.) the way you manage any other code — `git pull`, edit on your laptop, `git push`. The plugin keeps a running Hermes instance in sync with a git remote both directions:

- At the start of each Hermes session it `git fetch`es and rebases the agent's branch on top of `main`, so your latest hand-edits land in the volume before Hermes reads them.
- At the end of each session it commits anything the agent wrote (auto-skills, memory edits) and pushes to the agent's own branch.

Conflicts only surface when both you and the agent edit the same lines of the same file; otherwise rebases auto-resolve.

## How it works

```
main                ← you edit on your laptop and push here
hermes/<machine>    ← Hermes pushes here, reads from here on session start
```

The agent only ever writes to its own branch. Your edits live on `main` and flow into the agent on the next session_start via `git rebase origin/main`. Single-writer-per-branch means the agent's pushes always succeed; there's no rebase-and-retry dance at runtime.

| When | What |
|---|---|
| `on_session_start` | `git fetch`, `git rebase origin/main` onto agent-branch, `git reset --hard`. If rebase conflicts: abort, log, agent stays on previous state — you resolve on your laptop. |
| `on_session_end` | `git add -A` if dirty, `git commit`, `git push origin HEAD:<branch>`. |

## Secrets

The plugin does not encrypt or decrypt anything itself. If you want files in the state repo to be encrypted at rest in git, use [git-crypt](https://github.com/AGWA/git-crypt): commit a `.gitattributes` that points the matched paths at the `git-crypt` filter, and run `git-crypt unlock <keyfile>` once on each clone (laptop, container, CI). After that, `git add` automatically encrypts on commit (clean filter) and `git checkout` automatically decrypts on checkout (smudge filter), so the working tree is always plaintext and `.git/objects` / the remote always see ciphertext.

The plugin sees only the working tree, which is plaintext under git-crypt. Encryption is transparent to it.

A minimal `.gitattributes` for the state repo:

```gitattributes
.env filter=git-crypt diff=git-crypt
.env.* filter=git-crypt diff=git-crypt
auth.json filter=git-crypt diff=git-crypt
```

For the deployment, mount the symmetric key (`git-crypt export-key`) into the container and run `git-crypt unlock <keyfile>` after the initial fetch+reset and before the agent starts.

The previous `sops`-based version of the plugin is preserved on the [`sops` branch](https://github.com/tescalada/hermes-git-sync/tree/sops) for anyone still using SOPS + age.

## Installation

### Prerequisites

The plugin shells out to system `git` only (already in the upstream Hermes Docker image). For encryption you'll additionally want `git-crypt` available in the deployment image:

```dockerfile
FROM nousresearch/hermes-agent:latest
RUN apt-get update && apt-get install -y --no-install-recommends git-crypt \
    && rm -rf /var/lib/apt/lists/*
```

### Install the plugin

Add it to whichever uv project hosts your Hermes install:

```sh
uv add hermes-git-sync
```

Hermes auto-discovers it via the `hermes_agent.plugins` entry-point group on next startup.

## Configuration

Set these in your Hermes `.env` (or pass via container env vars):

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `HERMES_SYNC_REMOTE` | yes | — | Git remote URL for the state repo |
| `HERMES_SYNC_BRANCH` | no | `hermes/main` | Branch this instance owns |

`GIT_`, `SSH_`, and `GPG_` prefixed env vars are forwarded to the git subprocess, so set up SSH (or `GIT_SSH_COMMAND`, or a credential helper) as you would for any other git client.

### State repo setup

1. Create a private git repo on your preferred git host. Self-hosting keeps your state inside your own network.
2. Set up SSH (or HTTPS + credential helper) so `git clone <remote>` works without prompting.
3. Initialize the repo with a `main` branch and a `.gitignore`. If you want to encrypt secrets, add `.gitattributes` + run `git-crypt init` (see the Secrets section above).

### `.gitignore` for the state repo

Recommended deny-by-default policy so new file types Hermes invents don't accidentally leak:

```gitignore
# Deny everything by default
*
!.gitignore
!.gitattributes

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

# Allow-list secret paths covered by .gitattributes (git-crypt encrypts them
# on commit via the clean filter)
!.env
!.env.*
!auth.json

# Belt-and-suspenders deny of other secret-shaped paths not covered by .gitattributes
**/*.key
**/*.pem
**/*.token
**/secrets*
```

## License

MIT
