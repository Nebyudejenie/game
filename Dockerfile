# syntax=docker/dockerfile:1
#
# One image for every deployable unit in this monorepo (gateway, admin,
# payments -- uvicorn FastAPI apps; bot, engine worker, payout worker --
# python -m entrypoints) -- deploy/docker-compose.prod.yml gives each
# service its own `command:` against this same image, rather than
# maintaining six near-identical Dockerfiles for one shared Python
# environment.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# No build-essential/compiler needed -- confirmed directly, not assumed:
# every one of this project's C-extension dependencies (asyncpg,
# cryptography, bcrypt, psycopg2-binary) installs from a prebuilt
# manylinux wheel against this exact base image, verified via a real
# `docker build` of this file. One less system package (and its own
# network dependency on the distro's own package mirror) to install.
COPY . .

# Editable install -- the exact same method this project's own README
# documents for local dev (`pip install -e ".[dev]"`, just without the
# dev-only extras: pytest/mypy/playwright have no place in a production
# image). Deliberately not a "real" wheel build: services/gateway/app.py
# locates web/miniapp/ by a path relative to its own file location, which
# only survives if the source tree stays laid out exactly as it is in the
# repo -- an editable install (a .pth file pointing straight back at
# /app) preserves that; a real build installing into site-packages would
# not.
RUN pip install --no-cache-dir -e .

# A real non-root user -- the base image's own default is root, which
# means every one of this monorepo's six deployed services (a real
# -money gambling platform: gateway, admin, payments, bot, engine
# -worker, payout-worker) would otherwise run as root inside its
# container for no reason it actually needs to. A future vulnerability
# in any of them (a dependency CVE, an unforeseen bug) would then hand
# an attacker root inside the container instead of a deliberately
# unprivileged one -- standard container hardening, done after the pip
# install above (which needs to write to the base image's site
# -packages as root) and before the chown, so the app's own files are
# actually owned by the user that runs them.
RUN useradd --create-home --shell /usr/sbin/nologin jobingo && chown -R jobingo:jobingo /app
USER jobingo

# No default CMD -- deploy/docker-compose.prod.yml sets a real one per
# service.
