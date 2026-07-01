FROM mcr.microsoft.com/devcontainers/python:3.12-bookworm

# Install uv directly from the official image — no curl, no installer script.
# Pin the version for reproducible builds (bump deliberately).
COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /uvx /usr/local/bin/

# Ensure pip is available in the system Python. Recent updates to the base
# image ship without pip; pre-commit needs it to bootstrap its hook virtualenvs
# (uv virtualenvs omit pip by design, so pre-commit can't find it otherwise).
RUN python3.12 -m ensurepip --upgrade

# sshpass enables non-interactive SSH password auth so Claude can reach the
# appliance Pi (pi@partybox.local, password "raspberry") without key exchange.
RUN apt-get update -qq && apt-get install -y --no-install-recommends sshpass && rm -rf /var/lib/apt/lists/*
