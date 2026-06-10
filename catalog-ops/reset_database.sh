#!/bin/bash
################################################################################
# Reset HEC-RAS STAC Database for New Catalog Load
#
# Purpose: Completely reset the pgstac database to load a new STAC catalog.
# Usage: ./reset_database.sh [--force]
################################################################################

set -euo pipefail

DB_CONTAINER="hec-ras-stac-db"

FORCE=false
if [ "${1:-}" = "--force" ]; then
    FORCE=true
fi

echo "========================================"
echo "HEC-RAS STAC — Database Reset"
echo "========================================"
echo ""

if [ "$FORCE" = false ]; then
    echo "WARNING: This will DELETE all data in the pgstac database!"
    echo ""
    read -p "Are you sure you want to continue? (yes/no): " confirm

    if [ "$confirm" != "yes" ]; then
        echo "Aborted."
        exit 0
    fi
fi

echo ""
echo "[$(date)] Starting database reset..."
echo ""

echo "[$(date)] Step 1: Stopping services..."
docker-compose down
echo "[$(date)] Services stopped"
echo ""

echo "[$(date)] Step 2: Removing database data..."

if docker volume ls | grep -q "pgstac-data"; then
    echo "[$(date)] Removing named volume: pgstac-data"
    docker volume rm pgstac-data 2>/dev/null || true
else
    PGDATA_DIR="$(dirname "${BASH_SOURCE[0]}")/pgdata"
    if [ -d "$PGDATA_DIR" ]; then
        echo "[$(date)] Removing bind mount data: $PGDATA_DIR"
        rm -rf "$PGDATA_DIR"
    fi
fi

echo "[$(date)] Database data removed"
echo ""

echo "[$(date)] Step 3: Starting services with fresh database..."
docker-compose up -d
echo ""

echo "[$(date)] Step 4: Waiting for database initialization..."
MAX_WAIT=120
WAIT_COUNT=0
while [ $WAIT_COUNT -lt $MAX_WAIT ]; do
    if docker exec "$DB_CONTAINER" pg_isready -U pgstac -d stacdb >/dev/null 2>&1; then
        echo "[$(date)] Database is ready"
        break
    fi
    sleep 2
    WAIT_COUNT=$((WAIT_COUNT + 2))
    if [ $((WAIT_COUNT % 10)) -eq 0 ]; then
        echo "[$(date)] Still waiting... ($WAIT_COUNT/${MAX_WAIT}s)"
    fi
done

if [ $WAIT_COUNT -ge $MAX_WAIT ]; then
    echo "[$(date)] ERROR: Database failed to start within ${MAX_WAIT} seconds"
    echo "[$(date)] Check logs: docker logs $DB_CONTAINER"
    exit 1
fi
echo ""

echo "[$(date)] Step 5: Verifying pgstac installation..."
if docker exec "$DB_CONTAINER" psql -U pgstac -d stacdb -c "\dx" | grep -q "pgstac"; then
    echo "[$(date)] pgstac extension verified"
elif docker exec "$DB_CONTAINER" psql -U pgstac -d stacdb -c "SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'pgstac';" | grep -q "pgstac"; then
    echo "[$(date)] pgstac schema verified"
else
    echo "[$(date)] WARNING: pgstac not found in database"
    echo "[$(date)] Check logs: docker logs $DB_CONTAINER"
    exit 1
fi
echo ""

echo "[$(date)] Step 6: Database statistics (should be empty)..."
docker exec "$DB_CONTAINER" psql -U pgstac -d stacdb -c "SELECT COUNT(*) as collection_count FROM pgstac.collections;" 2>/dev/null || echo "No collections table yet"
docker exec "$DB_CONTAINER" psql -U pgstac -d stacdb -c "SELECT COUNT(*) as item_count FROM pgstac.items;" 2>/dev/null || echo "No items table yet"

echo ""
echo "========================================"
echo "Database Reset Complete!"
echo "========================================"
echo ""
echo "The database is now empty and ready to load a new catalog."
echo ""
echo "Next steps:"
echo "  1. Upload STAC JSONs to S3 (durable store):"
echo "     aws s3 sync ~/ras-stac-migration-v2/dest/ s3://hv-fim-dev-stac/hec-ras-stac/ --profile <dest-profile>"
echo ""
echo "  2. Load into pgSTAC:"
echo "     python3 load_catalog.py ~/ras-stac-migration-v2/dest --db-host localhost"
echo ""
echo "  3. Rewrite asset HREFs for browser access:"
echo "     python3 rewrite_asset_urls.py --proxy-url http://\$(hostname -I | awk '{print \$1}'):8083"
echo ""
