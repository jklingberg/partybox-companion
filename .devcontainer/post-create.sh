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

# Mark this checkout trusted for Codex CLI so the repo's committed
# .codex/config.toml (Auto mode: workspace-write sandbox + on-request
# approval) takes effect without an interactive "trust this folder?" prompt
# on first launch. Trust can't be granted from project-scoped config itself
# — that's a deliberate security boundary so an untrusted clone can't
# self-elevate — so it has to be provisioned here, in the user-level config.
codex_config=/home/vscode/.codex/config.toml
touch "$codex_config"
project_dir=$(pwd)
if ! grep -qF "[projects.\"$project_dir\"]" "$codex_config"; then
  printf '\n[projects."%s"]\ntrust_level = "trusted"\n' "$project_dir" >> "$codex_config"
fi
