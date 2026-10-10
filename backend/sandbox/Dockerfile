# Sandbox image for running a PR's linters and tests.
# Run with: no network, no host env, read-only root, non-root user (see runner.py).
FROM python:3.11-slim
RUN pip install --no-cache-dir pylint ruff pytest
# HOME on the tmpfs so tools that want a cache dir do not fail on the read-only root.
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
