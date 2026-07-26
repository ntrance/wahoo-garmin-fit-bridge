#!/usr/bin/env bash
set -euo pipefail

docker compose exec bridge python -m app.scanner --once

