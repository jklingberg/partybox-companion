#!/usr/bin/env bash
#
# Runs once after the dev container is first created (devcontainer.json
# postCreateCommand). Keep this idempotent — it may re-run on a rebuild.
set -euo pipefail

# The Claude and Codex config volumes mount as root (named volumes always
# do), so fix ownership before the vscode user tries to write
# credentials/sessions to them.
sudo chown -R vscode:vscode /home/vscode/.claude /home/vscode/.codex

# Install the full uv workspace (all packages + their dev extras).
uv sync --all-extras

# Install pre-commit as a uv tool and wire up the git hook.
uv tool install pre-commit
pre-commit install

# Install the no-direct-push-to-main hook. The source lives in .githooks/ so
# it is tracked in the repo; post-create copies it into .git/hooks/ at setup.
install -m 755 .githooks/pre-push .git/hooks/pre-push

# Install Claude Code globally.
npm install -g @anthropic-ai/claude-code

# Install OpenAI Codex CLI globally.
npm install -g @openai/codex

# Default Codex to its built-in "Auto" mode (workspace-write sandbox +
# on-request approval) so it doesn't stop to ask which mode to run in on
# first launch. It can still read/edit/run commands in the repo without
# prompting, but still asks before touching anything outside the workspace
# or hitting the network. Codex may have already created config.toml itself
# (e.g. a [projects."..."] trust_level entry from an earlier run), so check
# for the specific keys rather than file existence, and prepend rather than
# overwrite — TOML requires top-level keys to precede any [table] section.
codex_config=/home/vscode/.codex/config.toml
touch "$codex_config"
if ! grep -q '^approval_policy' "$codex_config"; then
  printf 'approval_policy = "on-request"\nsandbox_mode = "workspace-write"\n\n' \
    | cat - "$codex_config" > "$codex_config.tmp"
  mv "$codex_config.tmp" "$codex_config"
fi
