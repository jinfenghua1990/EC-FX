#!/bin/sh
set -eu
PROJECT_DIR="${PROJECT_DIR:-/volume1/docker/ecommerce-reconciliation}"
cd "$PROJECT_DIR"
exec /bin/sh scripts/rollback.sh --yes
