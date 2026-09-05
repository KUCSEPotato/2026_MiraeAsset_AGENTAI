#!/usr/bin/env bash
set -euo pipefail
python3 tests/frontend/container_smoke.py "${1:?frontend image is required}"
