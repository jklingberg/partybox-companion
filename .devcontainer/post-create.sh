#!/usr/bin/env bash
#
# Runs once after the dev container is first created (devcontainer.json
# postCreateCommand). Keep this idempotent — it may re-run on a rebuild.
set -euo pipefail

# The Claude config volume mounts as root (named volumes always do), so fix
# ownership before the vscode user tries to write credentials/sessions to it.
sudo chown -R vscode:vscode /home/vscode/.claude

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
