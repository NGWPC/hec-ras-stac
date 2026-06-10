#!/bin/bash
################################################################################
# Test Asset-Proxy Service
#
# Smoke-tests the asset-proxy with a real asset from the pgSTAC database.
# Usage: ./test_asset_proxy.sh [proxy-base-url]
#   Default proxy URL: http://localhost:8083
################################################################################

set -euo pipefail

DB_CONTAINER="hec-ras-stac-db"
PROXY_CONTAINER="hec-ras-stac-asset-proxy"
PROXY_BASE="${1:-http://localhost:8083}"

echo "========================================"
echo "HEC-RAS STAC — Asset Proxy Test"
echo "========================================"
echo "Proxy base: $PROXY_BASE"
echo ""

test_url() {
    local url="$1"
    local description="$2"

    echo "Testing: $description"
    echo "URL: $url"
    echo ""
    curl -sI -L "$url" 2>&1 | head -15
    echo ""

    FINAL_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -L "$url" 2>&1)
    if [ "$FINAL_STATUS" = "200" ]; then
        echo "SUCCESS (HTTP 200)"
    else
        echo "FAILED (HTTP $FINAL_STATUS)"
    fi
    echo ""
    echo "----------------------------------------"
    echo ""
}

# Test 1: Health check
echo "Test 1: Health Check"
echo "---------------------"
if curl -s "$PROXY_BASE/health" | grep -q "ok"; then
    echo "Asset-proxy is running"
    curl -s "$PROXY_BASE/health" | jq '.' 2>/dev/null || curl -s "$PROXY_BASE/health"
    echo ""
else
    echo "Asset-proxy NOT responding at $PROXY_BASE/health"
    echo ""
    docker ps | grep ras-stac || echo "No ras-stac containers running"
    echo ""
    echo "Recent logs:"
    docker logs --tail 30 "$PROXY_CONTAINER" 2>&1 || echo "Cannot get logs"
    exit 1
fi
echo "----------------------------------------"
echo ""

# Test 2: AWS credentials inside proxy container
echo "Test 2: AWS Credentials"
echo "------------------------"
if docker exec "$PROXY_CONTAINER" python3 -c \
    "import boto3; print('Region:', boto3.Session().region_name); print('Identity:', boto3.client('sts').get_caller_identity())" 2>/dev/null; then
    echo "AWS credentials working inside proxy container"
else
    echo "AWS credentials NOT working — check IAM instance profile"
fi
echo ""
echo "----------------------------------------"
echo ""

# Test 3: Pull a sample asset from the database and test it through the proxy
echo "Test 3: Sample Asset via Proxy"
echo "-------------------------------"

if [ -n "${PGPASSWORD:-}" ]; then
    : # already set
elif [ -f /opt/ras-stac/.db_password ]; then
    PGPASSWORD=$(cat /opt/ras-stac/.db_password)
else
    echo "WARNING: PGPASSWORD not set and /opt/ras-stac/.db_password not found"
    echo "Skipping database asset test"
    exit 0
fi
export PGPASSWORD

SAMPLE_ASSET=$(docker exec -i "$DB_CONTAINER" psql -U pgstac -d stacdb -t <<'SQL'
SELECT asset_value->>'href'
FROM pgstac.items,
LATERAL jsonb_each(content->'assets') AS assets(asset_key, asset_value)
WHERE asset_value->>'href' IS NOT NULL
LIMIT 1;
SQL
)

if [ -z "$SAMPLE_ASSET" ]; then
    echo "No assets found in database — is the catalog loaded?"
    exit 1
fi

SAMPLE_ASSET=$(echo "$SAMPLE_ASSET" | tr -d ' ')
echo "Sample asset HREF: $SAMPLE_ASSET"
echo ""

if [[ "$SAMPLE_ASSET" == s3://* ]]; then
    BUCKET=$(echo "$SAMPLE_ASSET" | sed 's|s3://||' | cut -d'/' -f1)
    KEY=$(echo "$SAMPLE_ASSET" | sed 's|s3://||' | cut -d'/' -f2-)
    echo "Bucket: $BUCKET"
    echo "Key:    $KEY"
    echo ""
    PROXY_URL="$PROXY_BASE/s3/$BUCKET/$KEY"
    test_url "$PROXY_URL" "S3 asset via proxy"

elif [[ "$SAMPLE_ASSET" == "$PROXY_BASE"/s3/* ]]; then
    test_url "$SAMPLE_ASSET" "Existing proxy URL"

elif [[ "$SAMPLE_ASSET" == http://*:8083/s3/* ]]; then
    LOCAL_URL=$(echo "$SAMPLE_ASSET" | sed "s|http://[^:]*:8083|$PROXY_BASE|")
    test_url "$LOCAL_URL" "Proxy URL (normalized to $PROXY_BASE)"
fi

echo "========================================"
echo "Test Complete"
echo "========================================"
echo ""
echo "If assets are still S3 URIs, rewrite them:"
echo "  python3 rewrite_asset_urls.py --proxy-url \$(hostname -I | awk '{print \$1}' | sed 's/ //'| xargs -I{} echo 'http://{}:8083')"
echo ""
echo "If proxy fails, check logs:"
echo "  docker logs $PROXY_CONTAINER"
echo ""
