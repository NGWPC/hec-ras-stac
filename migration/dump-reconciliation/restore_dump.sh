#!/bin/bash
# Spin a local pgstac container, restore the Dewberry pgstac dump into it,
# leave it running on localhost:5433 for build_drop_list.py and other probes.
#
# Idempotent: tears down any prior container of the same name first.
#
# Usage:
#   ./restore_dump.sh                                  # default dump path
#   ./restore_dump.sh /path/to/some.dump               # override
set -euo pipefail

CONTAINER="${CONTAINER:-dewberry-pgstac-probe}"
IMAGE="${IMAGE:-ghcr.io/stac-utils/pgstac:v0.7.10}"
PORT="${PORT:-5433}"
# Default dump path resolves relative to this script: ../../20260518_183642.dump
# (script lives at migration/dump-reconciliation/, dump at repo root).
DEFAULT_DUMP="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/20260518_183642.dump"
DUMP="${1:-$DEFAULT_DUMP}"

if [ ! -f "$DUMP" ]; then
    echo "ERROR: dump file not found at $DUMP" >&2
    exit 1
fi

echo "==> Tearing down any prior container named $CONTAINER"
docker rm -f "$CONTAINER" 2>/dev/null || true

echo "==> Starting pgstac container ($IMAGE) on port $PORT"
# pgstac's init script uses libpq env vars (PGUSER/PGPASSWORD/PGDATABASE),
# not just POSTGRES_*. Both sets are required or the init crashes with
# "role 'postgres' does not exist" and the container exits.
docker run -d --name "$CONTAINER" \
    -e POSTGRES_USER=pgstac \
    -e POSTGRES_PASSWORD=pgstac \
    -e POSTGRES_DB=stacdb \
    -e PGUSER=pgstac \
    -e PGPASSWORD=pgstac \
    -e PGDATABASE=stacdb \
    -p "$PORT:5432" \
    "$IMAGE" >/dev/null

echo "==> Waiting for pgstac init to complete (can take 4-5 min on first boot)"
# Wait for init to fully complete. The pgstac image init script:
#   1. Starts Postgres (pgstac tables exist briefly)
#   2. Runs /docker-entrypoint-initdb.d/ scripts
#   3. Restarts Postgres (connection briefly drops)
# We wait for the items table, then wait for the restart to settle, then
# verify one final time to confirm the post-restart Postgres is accepting
# connections and the schema is still in place.
READY_SQL="SELECT 1 FROM information_schema.tables WHERE table_schema='pgstac' AND table_name='items' LIMIT 1"
echo -n "    waiting"
for i in $(seq 1 300); do
    if ! docker ps -q --filter "name=^${CONTAINER}$" | grep -q .; then
        echo ""
        echo "ERROR: container exited during init" >&2
        docker logs --tail 60 "$CONTAINER" >&2
        exit 1
    fi
    if docker exec "$CONTAINER" psql -U pgstac -d stacdb -tAc "$READY_SQL" 2>/dev/null | grep -q 1; then
        echo ""
        echo "    pgstac tables visible after ${i}s, waiting for post-init restart to settle (10s)"
        sleep 10
        break
    fi
    echo -n "."
    sleep 1
done
# Final check: confirm schema is accessible after any restart
if ! docker exec "$CONTAINER" psql -U pgstac -d stacdb -tAc "$READY_SQL" 2>/dev/null | grep -q 1; then
    echo "ERROR: pgstac schema not accessible after init + settle" >&2
    docker logs --tail 60 "$CONTAINER" >&2
    exit 1
fi
echo "    pgstac schema ready"

echo "==> Patching collection_geom search_path if present (dump's SET search_path='' breaks PostGIS resolution)"
docker exec "$CONTAINER" psql -U pgstac -d stacdb -c "
DO \$\$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid
               WHERE n.nspname = 'pgstac' AND p.proname = 'collection_geom') THEN
        ALTER FUNCTION pgstac.collection_geom(jsonb) SET search_path = pgstac, public;
    END IF;
END \$\$;" 2>&1

echo "==> Copying dump into container (this is the slow part: $(du -h "$DUMP" | cut -f1))"
docker cp "$DUMP" "$CONTAINER:/tmp/dewberry.dump"

echo "==> Running pg_restore (--no-owner --no-acl, will emit some warnings on existing pgstac roles/extensions)"
docker exec "$CONTAINER" pg_restore \
    -U pgstac \
    -d stacdb \
    --no-owner \
    --no-acl \
    --verbose \
    /tmp/dewberry.dump 2>&1 | tail -40 || {
    echo "WARN: pg_restore emitted non-zero; checking smoke counts anyway" >&2
}

echo
echo "==> Smoke checks"
docker exec "$CONTAINER" psql -U pgstac -d stacdb -c \
    "SELECT count(*) AS collections FROM pgstac.collections;"
docker exec "$CONTAINER" psql -U pgstac -d stacdb -c \
    "SELECT count(*) AS items FROM pgstac.items;"
docker exec "$CONTAINER" psql -U pgstac -d stacdb -c \
    "SELECT collection, count(*) AS n FROM pgstac.items GROUP BY collection ORDER BY n DESC LIMIT 10;"

echo
echo "==> Container '$CONTAINER' is up on localhost:$PORT"
echo "    user=pgstac password=pgstac db=stacdb"
echo "    psql:    docker exec -it $CONTAINER psql -U pgstac -d stacdb"
echo "    teardown: docker rm -f $CONTAINER"
