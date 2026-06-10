#!/bin/bash
################################################################################
# Diagnose Asset Display Issues
#
# Helps troubleshoot why thumbnails/assets aren't showing in STAC Browser.
################################################################################

set -euo pipefail

DB_CONTAINER="hec-ras-stac-db"
PROXY_CONTAINER="hec-ras-stac-asset-proxy"

echo "========================================"
echo "HEC-RAS STAC — Asset Diagnostics"
echo "========================================"
echo ""

# Resolve database password
if [ -n "${PGPASSWORD:-}" ]; then
    : # already set
elif [ -f /opt/ras-stac/.db_password ]; then
    PGPASSWORD=$(cat /opt/ras-stac/.db_password)
else
    echo "ERROR: Could not find database password"
    echo "Set PGPASSWORD or place password in /opt/ras-stac/.db_password"
    exit 1
fi
export PGPASSWORD

echo "1. Checking Docker Services"
echo "----------------------------"
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep ras-stac || echo "No ras-stac containers running!"
echo ""

echo "2. Checking Asset-Proxy Service"
echo "--------------------------------"
if curl -s http://localhost:8083/health | grep -q "ok"; then
    echo "Asset-proxy is running"
    curl -s http://localhost:8083/health | jq '.' 2>/dev/null || curl -s http://localhost:8083/health
else
    echo "Asset-proxy NOT responding at http://localhost:8083/health"
    echo ""
    echo "Checking logs:"
    docker logs --tail 20 "$PROXY_CONTAINER" 2>&1 || echo "Container not found"
fi
echo ""

echo "3. Checking STAC API"
echo "--------------------"
if curl -s http://localhost:8082/ | grep -q "stac_version"; then
    echo "STAC API is running"
else
    echo "STAC API NOT responding at http://localhost:8082"
fi
echo ""

echo "4. Sample Asset URLs from Database"
echo "-----------------------------------"
docker exec -i "$DB_CONTAINER" psql -U pgstac -d stacdb <<'SQL'
SELECT
    collection,
    id,
    jsonb_pretty(content->'assets') as assets
FROM pgstac.items
WHERE jsonb_typeof(content->'assets') = 'object'
LIMIT 1;
SQL
echo ""

echo "5. Asset URL Pattern Summary"
echo "-----------------------------"
docker exec -i "$DB_CONTAINER" psql -U pgstac -d stacdb <<'SQL'
WITH asset_urls AS (
    SELECT
        asset_value->>'href' as href
    FROM pgstac.items,
    LATERAL jsonb_each(content->'assets') AS assets(asset_key, asset_value)
    WHERE jsonb_typeof(content->'assets') = 'object'
    LIMIT 500
)
SELECT
    CASE
        WHEN href LIKE 's3://%'                   THEN 'S3 URI (needs rewriting)'
        WHEN href LIKE '%s3.amazonaws.com%'        THEN 'S3 HTTPS (needs rewriting)'
        WHEN href LIKE 'http://localhost:8083/s3/%' THEN 'Proxy URL (localhost)'
        WHEN href LIKE 'http://%:8083/s3/%'        THEN 'Proxy URL (external)'
        ELSE 'Other'
    END as url_type,
    COUNT(*) as count,
    MIN(href) as example
FROM asset_urls
GROUP BY url_type
ORDER BY count DESC;
SQL
echo ""

echo "6. Testing Thumbnail Access"
echo "----------------------------"
SAMPLE_THUMBNAIL=$(docker exec -i "$DB_CONTAINER" psql -U pgstac -d stacdb -t <<'SQL'
SELECT asset_value->>'href'
FROM pgstac.items,
LATERAL jsonb_each(content->'assets') AS assets(asset_key, asset_value)
WHERE asset_key = 'thumbnail'
  AND asset_value->>'href' IS NOT NULL
LIMIT 1;
SQL
)

if [ -n "$SAMPLE_THUMBNAIL" ]; then
    SAMPLE_THUMBNAIL=$(echo "$SAMPLE_THUMBNAIL" | tr -d ' ')
    echo "Sample thumbnail: $SAMPLE_THUMBNAIL"
    echo ""

    if [[ "$SAMPLE_THUMBNAIL" == http* ]]; then
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -L "$SAMPLE_THUMBNAIL" 2>&1)
        echo "HTTP Status: $HTTP_CODE"
        if [ "$HTTP_CODE" = "200" ]; then
            echo "Asset is accessible"
        else
            echo "Asset returned error: $HTTP_CODE"
            curl -I "$SAMPLE_THUMBNAIL" 2>&1 | head -15
        fi
    elif [[ "$SAMPLE_THUMBNAIL" == s3://* ]]; then
        echo "Asset URL is still S3 URI — needs rewriting"
        echo "Run: python3 rewrite_asset_urls.py --proxy-url http://localhost:8083"
    fi
else
    echo "No thumbnail assets found in database"
fi
echo ""

echo "7. AWS Credentials"
echo "-------------------"
if aws sts get-caller-identity &>/dev/null; then
    echo "AWS credentials available"
    aws sts get-caller-identity
else
    echo "No AWS credentials found — check IAM instance profile"
fi
echo ""

echo "8. Access URLs"
echo "---------------"
HOST_IP=$(hostname -I | awk '{print $1}')
echo "STAC Browser: http://$HOST_IP:8080"
echo "STAC API:     http://$HOST_IP:8082"
echo "Asset Proxy:  http://$HOST_IP:8083"
echo ""

echo "========================================"
echo "Diagnostics Complete"
echo "========================================"
echo ""
echo "Common fixes:"
echo ""
echo "  Asset URLs still show 's3://':"
echo "    python3 rewrite_asset_urls.py --proxy-url http://\$(hostname -I | awk '{print \$1}'):8083"
echo ""
echo "  Asset-proxy not running:"
echo "    docker logs $PROXY_CONTAINER"
echo "    docker-compose restart asset-proxy"
echo ""
echo "  Proxy URLs use 'localhost' but accessing from remote browser:"
echo "    Rerun rewrite_asset_urls.py with the EC2 external IP as --proxy-url"
echo ""
