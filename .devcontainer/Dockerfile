FROM mcr.microsoft.com/devcontainers/python:3.12-bookworm

# Install uv directly from the official image — no curl, no installer script.
# Pin the version for reproducible builds (bump deliberately).
COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /uvx /usr/local/bin/
