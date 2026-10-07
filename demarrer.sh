#!/bin/sh
# Changez le mot de passe avant la mise en service
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-ChangezMoi2026}"
export PORT="${PORT:-8080}"
cd "$(dirname "$0")" && exec python3 server.py
