#!/bin/sh
set -eu
PROJECT_DIR="${PROJECT_DIR:-/volume1/docker/ecommerce-reconciliation}"
PATH="/usr/local/bin:/usr/bin:/bin:/usr/syno/bin:/var/packages/ContainerManager/target/usr/bin:${PATH:-}"
export PATH PROJECT_DIR
cd "$PROJECT_DIR"
exec /bin/sh scripts/web-update-worker.sh
