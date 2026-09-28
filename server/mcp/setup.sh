#!/usr/bin/env bash
# Build the pinned MCP server venvs. Safe to re-run.
#   venv/      Roku + Govee servers (system python3, MCP SDK 1.x)
#   venv-atv/  Apple TV server (Python 3.13 via uv, MCP SDK 2.x)
set -euo pipefail
cd "$(dirname "$0")"
[ -x venv/bin/python3 ] || python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt uv
venv/bin/pip freeze | grep -iE '^(mcp|govee-mcp|mcp-remote-control)=='

[ -x venv-atv/bin/python ] || venv/bin/uv venv -q -p 3.13 venv-atv
venv/bin/uv pip install -q --python venv-atv/bin/python -r requirements-atv.txt
venv/bin/uv pip freeze --python venv-atv/bin/python | grep -iE '^(mcp|mcp-pyatv|pyatv)=='
