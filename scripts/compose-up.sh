#!/bin/bash
# scripts/compose-up.sh - bring up the project's MinIO compose stack if it is
# not already running, then wait for the bucket-init helper to finish so
# callers can rely on the endpoint (http://localhost:9000) being fully
# provisioned (s3bak-test created). Re-running it is safe.
#
# For raw compose subcommands (down, ps, logs, ...) use scripts/compose.sh.

set -euo pipefail

scriptdir=$(cd "$(dirname "$0")" && pwd)
compose="$scriptdir/compose.sh"

echo 'Starting docker minio'
# Always ask Compose to reconcile both services. A minio-container-only check
# can race another startup or overlook a failed/missing mc-init container and
# return before the bucket exists. `up -d` and `mc mb --ignore-existing` are
# idempotent when the stack is already ready.
if ! "$compose" up -d; then
    echo "$0: docker start failed" >&2
    exit 1
fi

mc_init_cid=$("$compose" ps -aq mc-init 2>/dev/null)
if [ -z "$mc_init_cid" ]; then
    echo "$0: mc-init container was not created" >&2
    exit 1
fi
if ! mc_init_rc=$(timeout 30 docker wait "$mc_init_cid"); then
    echo "$0: timed out waiting for mc-init (30s); bucket may not be ready" >&2
    exit 1
fi
if [ "$mc_init_rc" != "0" ]; then
    echo "$0: mc-init exited with status $mc_init_rc; bucket may not be ready" >&2
    exit 1
fi
