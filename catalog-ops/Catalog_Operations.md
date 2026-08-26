# HEC-RAS STAC: Catalog Operations

> **Prerequisites:** Complete `deployment/Deployment_Runbook.md` first
> (Phase 2.3 covers connecting to the instance and Phase 2.4 clones this repo to
> `/opt/hec-ras-stac/repo`). Before starting, you should be connected to the
> instance (SSH or Session Manager) with all four containers healthy
> (`docker ps` shows hec-ras-stac-db, hec-ras-stac-api, hec-ras-stac-browser,
> hec-ras-stac-asset-proxy) and the repo cloned.

Covers syncing the STAC catalog from S3, loading it into pgSTAC, rewriting
asset URLs for browser access, and verifying the asset proxy end-to-end. All
commands run on the EC2 instance.

## Overview

| | |
|---|---|
| STAC bucket | `s3://hv-fim-dev-stac/hec-ras-stac/` |
| Data bucket | `s3://hv-fim-dev-data/hec-ras/` |
| Catalog scale | 166,607 items, 1,431 collections (`ble_*`, `mip_*`, `ohio_rfc`, `mn_*`, `nc_*`) |

Scripts live at `/opt/hec-ras-stac/repo/catalog-ops/`, cloned from `https://github.com/NGWPC/hec-ras-stac` (`catalog-ops` branch) in Deployment Runbook Phase 2.4.

---

## Phase 1: Sync STAC Catalog Locally

The catalog JSONs were copied into `hv-fim-dev-stac` during Deployment Phase 1,
with HREFs and thumbnail structure already corrected. Sync them locally for loading.

### 1.1 Sync Catalog Locally
```bash
mkdir -p ~/hec-ras-catalog
aws s3 sync s3://hv-fim-dev-stac/hec-ras-stac/ ~/hec-ras-catalog/
```

Expected: ~168,039 objects (catalog.json, 6 program catalogs, 1,431
collection.json files, 166,607 item JSONs). Takes a few minutes.

### 1.2 Verify Sync
```bash
ls ~/hec-ras-catalog/
# Expected: catalog.json  ble/  mip/  ohio_rfc/ mn/ nc/

# Spot-check counts
find ~/hec-ras-catalog -name "collection.json" | wc -l   # 1431
find ~/hec-ras-catalog -name "*.json" \
  ! -name "catalog.json" ! -name "collection.json" | wc -l  # 166607
```

---

## Phase 2: Catalog Loading

Script: `catalog-ops/load_catalog.py`

### 2.1 Dry Run
```bash
export PGPASSWORD=$(sudo cat /opt/hec-ras-stac/.db_password)

python3 /opt/hec-ras-stac/repo/catalog-ops/load_catalog.py \
  ~/hec-ras-catalog --db-host localhost --dry-run
```

Expected output: `Layout: destination`, `Collections: 1431`, `Items: 166607`,
no "has no collection field" warnings.

### 2.2 Load

Run in `tmux` or `screen` — the full 166k load takes ~1 hour.

```bash
sudo python3 /opt/hec-ras-stac/repo/catalog-ops/load_catalog.py \
  ~/hec-ras-catalog --db-host localhost
```

If the terminal disconnects mid-run, verify completion via DB counts in 2.3 — the SUMMARY at the end may be lost if the session drops.

### 2.3 Verify
```bash
# DB counts
docker exec -i hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "SELECT COUNT(*) FROM pgstac.collections;"   # 1431

docker exec -i hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "SELECT COUNT(*) FROM pgstac.items;"          # 166607

# Verify no unexpected collections (should return 0 rows)
docker exec -i hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "SELECT id FROM pgstac.collections
   WHERE id NOT LIKE 'ble_%'
   AND id NOT LIKE 'mip_%'
   AND id NOT LIKE 'mn_%'
   AND id NOT LIKE 'nc_%'
   AND id != 'ohio_rfc'
   ORDER BY id;"

# Spot-check per-collection DB count vs S3 for representative collections
for col in ohio_rfc ble_05119_Pulaski mip_03160109; do
  s3_count=$(aws s3 ls s3://hv-fim-dev-stac/hec-ras-stac/ --recursive | grep "/${col}/" | grep '\.json$' | grep -v 'collection.json' | grep -v 'catalog.json' | wc -l)
  db_count=$(docker exec -i hec-ras-stac-db psql -U pgstac -d stacdb -t -A -c "SELECT COUNT(*) FROM pgstac.items WHERE collection='${col}';")
  echo "${col}: S3=${s3_count} DB=${db_count} $([ "$s3_count" -eq "$db_count" ] && echo 'OK' || echo 'MISMATCH')"
done
```

**Rollback:** Reset DB and re-load:
```bash
cd /opt/hec-ras-stac/deployment
sudo bash /opt/hec-ras-stac/repo/catalog-ops/reset_database.sh --force
# Then repeat Phase 2.
```

---

## Phase 3: Asset URL Rewriting

Script: `catalog-ops/rewrite_asset_urls.py`

Item JSONs in pgSTAC have `s3://` HREFs. The asset proxy streams S3 content
using the EC2 IAM role — browsers can't use IAM credentials directly. This step
rewrites every asset HREF to a proxy URL in a single SQL UPDATE.

### 3.0 Prerequisite: max_locks_per_transaction

The rewrite triggers pgSTAC partition updates that require more locks than
PostgreSQL's default allows. If this setting isn't applied you will see:

```
ERROR: out of shared memory
HINT: You might need to increase max_locks_per_transaction.
CONTEXT: SQL statement "REFRESH MATERIALIZED VIEW partitions"
```

Verify and apply before running the rewrite:

```bash
# Check current value (should be 256)
docker exec hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "SHOW max_locks_per_transaction;"

# If not 256, apply and restart
docker exec hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "ALTER SYSTEM SET max_locks_per_transaction = 256;"
docker restart hec-ras-stac-db

# Wait for DB to come back
until docker exec hec-ras-stac-db pg_isready -U pgstac -d stacdb >/dev/null 2>&1; do sleep 2; done
echo "DB ready"
```

This is applied automatically by `reset_database.sh`, but must be set manually
if the DB was not reset before loading.

### 3.1 Dry Run
```bash
export HOST_IP=$(hostname -I | awk '{print $1}')
export PGPASSWORD=$(sudo cat /opt/hec-ras-stac/.db_password)

sudo -E python3 /opt/hec-ras-stac/repo/catalog-ops/rewrite_asset_urls.py \
  --proxy-url http://${HOST_IP}:8083 \
  --db-host localhost --dry-run
```

Expected: `Items needing rewrite: <N>` followed by `[DRY RUN]`.

### 3.2 Apply
```bash
export HOST_IP=$(hostname -I | awk '{print $1}') 
sudo -E python3 /opt/hec-ras-stac/repo/catalog-ops/rewrite_asset_urls.py \
  --proxy-url http://${HOST_IP}:8083 \
  --db-host localhost --batch
```

`--batch` processes each collection prefix (`ble_*`, `mip_*`, `ohio_rfc`) in a
separate transaction to avoid lock exhaustion on large catalogs.

Expected: `Items updated: <N>` per prefix. Idempotent — re-running
when nothing needs rewriting prints `Nothing to do.`

### 3.3 Verify
```bash
# Confirm no raw s3:// HREFs remain (should return 0)
docker exec -i hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "SELECT COUNT(*) FROM pgstac.items
   WHERE (content->'assets')::text LIKE '%s3://%';"

# Spot-check proxy URL format
docker exec -i hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "SELECT content->'assets'->'thumbnail'->>'href'
   FROM pgstac.items WHERE content->'assets' ? 'thumbnail' LIMIT 3;"
# Expected: http://<HOST_IP>:8083/s3/<bucket>/hec-ras/...
```

---

## Phase 4: Post-Deployment

### 4.1 Service Health

```bash
# All 4 containers running
docker ps --format 'table {{.Names}}\t{{.Status}}' | grep hec-ras-stac
# Expected: 4 containers Up

# Health check script
/opt/hec-ras-stac/deployment/health-check.sh

# Browser reachable
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/
# Expected: 200
```

### 4.2 STAC API Endpoint Validation (`:8082`)

```bash
export HOST_IP=$(hostname -I | awk '{print $1}')

# Root, conformance
curl -s http://${HOST_IP}:8082/ | jq '.title'                           # "HEC-RAS STAC"
curl -s http://${HOST_IP}:8082/conformance | jq '.conformsTo | length'  # 26

# Search
curl -s "http://${HOST_IP}:8082/search?limit=10" | jq '.features | length'  # 10
time curl -s "http://${HOST_IP}:8082/search?bbox=-90,30,-80,40&limit=10" | jq '.features | length'
```

### 4.3 Asset Proxy Smoke Test

```bash
bash /opt/hec-ras-stac/repo/catalog-ops/test_asset_proxy.sh
```

Covers: proxy health endpoint, IAM credentials (STS caller identity), sample asset queryable from DB, direct S3 access via IAM role, proxy URL serves assets with correct Content-Type.

### 4.4 External Access

> From a workstation. Confirms security group allows the ports.

```bash
export EC2_IP=<EC2_IP>
curl -s -o /dev/null -w "API:     %{http_code}\n" http://${EC2_IP}:8082/
curl -s -o /dev/null -w "Browser: %{http_code}\n" http://${EC2_IP}:8080/
curl -s -o /dev/null -w "Proxy:   %{http_code}\n" http://${EC2_IP}:8083/health
# Expected: 200 for all three
```

### 4.5 STAC Browser UI

Open `http://<domain>:8080` in a browser. Verify:
- Collections list renders (1,431 collections)
- Navigating into a collection shows items with geometry on the map
- Expanding an asset and clicking Download streams the file (not 403)

If the map panel is blank in Chrome, see [STAC Browser — WebGL map not rendering](#stac-browser--webgl-map-not-rendering-chrome) under Troubleshooting.

### 4.6 QGIS Verification

> From a workstation with QGIS installed.

Install the STAC API Browser plugin: **Plugins > Manage and Install Plugins > search "STAC API Browser"**

1. **Plugins > STAC API Browser Plugin > Open STAC API Browser**
2. Add connection: **New > URL:** `http://<domain or HOST_IP>:8082`
3. Option 1: Extent dropdown: **Draw on Canvas** highlight an area with known collections, click **Search**
4. Option 2: Collections dropdown, Fetch Collections, Filter Collections (e.g. `mip_03160109`), click **Search**
5. Click select footprint checkbox, click Add selected or Add all footprints
6. Verify footprint renders on the map canvas
7. Back to STAC API Browser, you can also View assets, and download
8. Verify the layer loads with correct spatial extent

**Troubleshooting**

If the plugin fails with a pydantic `BaseSettings` error, see [QGIS STAC plugin — pydantic `BaseSettings` error](#qgis-stac-plugin--pydantic-basesettings-error) in the Troubleshooting section.

### 4.7 Initial Database Backup
```bash
/opt/hec-ras-stac/deployment/backup-db.sh
```

Automated weekly backups are configured (Sunday 2 AM) by the bootstrap. If the database is static after the initial load, you can disable the weekly schedule while keeping the script available for manual runs:

```bash
crontab -l | grep -v backup-db.sh | crontab -

# Verify removed
crontab -l
```

---

## Phase 5: Performance

Install Apache Bench if not present:
```bash
sudo apt-get install -y apache2-utils
```

```bash
export HOST_IP=$(hostname -I | awk '{print $1}')

# Collections endpoint — 1000 requests, 10 concurrent
ab -n 1000 -c 10 http://${HOST_IP}:8082/collections
# Check: "Failed requests: 0"

# Search endpoint — responses are ~144KB; latency will be 1-2s, that's expected
ab -n 500 -c 10 "http://${HOST_IP}:8082/search?limit=10"
# Check: "Failed requests: 0"

# bbox search
ab -n 500 -c 10 "http://${HOST_IP}:8082/search?bbox=-90,30,-80,40&limit=10"
# Check: "Failed requests: 0"
```

---

## Phase 6: Monitoring & Ops

Systemd service enabled and active:
```bash
systemctl is-enabled hec-ras-stac   # Expect: "enabled"
systemctl is-active hec-ras-stac    # Expect: "active"
```

Backup cron job — verify Sunday 2 AM schedule:
```bash
crontab -l | grep backup-db.sh
# Expect: line containing "0 2 * * 0" and backup-db.sh
```

S3 backup upload (if configured):
```bash
aws s3 ls s3://hv-fim-dev-data/hec-ras/backups/stac-db/
# Expect: backup files with recent timestamps
```

Logs — verify log directory and recent writes:
```bash
ls -la /var/log/hec-ras-stac/
tail -5 /var/log/hec-ras-stac/bootstrap.log
# Expect: log directory exists with recent log files
```

Docker container logs — no errors:
```bash
docker logs --tail 20 hec-ras-stac-api 2>&1 | grep -i error
docker logs --tail 20 hec-ras-stac-db 2>&1 | grep -i error
# Expect: empty output (no errors)
```

System resources:
```bash
df -h /
free -h
# Expect: adequate free disk and memory
```

---

## Phase 7: Production Readiness Sign-Off

**Infrastructure**
- [ ] EC2 instance provisioned with correct IAM role (`s3_read_paths` includes `hv-fim-dev-data` and `hv-fim-dev-stac`)
- [ ] Security groups configured (SSH restricted, ports 8080/8082/8083 accessible to intended users)
- [ ] Elastic IP or DNS configured (optional)

**Catalog & Assets**
- [ ] S3 catalog synced (`hv-fim-dev-stac/hec-ras-stac/`) — 166,607 items, 1,431 collections
- [ ] Asset HREFs rewritten to proxy URLs (no `s3://` HREFs remain in pgSTAC)

**Application**
- [ ] All 4 containers running: `hec-ras-stac-db`, `hec-ras-stac-api`, `hec-ras-stac-browser`, `hec-ras-stac-asset-proxy`
- [ ] STAC API responding on port 8082
- [ ] STAC Browser accessible on port 8080
- [ ] Asset proxy running on port 8083

**Testing**
- [ ] Health check script passes (`/opt/hec-ras-stac/deployment/health-check.sh`)
- [ ] API endpoints validated (root, conformance, collections, search, bbox)
- [ ] DB item count matches S3 catalog (166,607 items, 1,431 collections)
- [ ] Asset proxy test passes (`test_asset_proxy.sh`)
- [ ] QGIS plugin connects and renders footprints

**Operations**
- [ ] Systemd service enabled for auto-start
- [ ] Automated backups scheduled and manually tested
- [ ] Documentation updated with actual IPs/endpoints

---

## Troubleshooting

### QGIS STAC plugin — pydantic `BaseSettings` error

Install the missing dependency:
```bash
pip install pydantic-settings
```

Then patch `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/qgis_stac/lib/planetary_computer/settings.py`:
```python
# Change:
class Settings(pydantic.BaseSettings):
# To:
from pydantic_settings import BaseSettings
class Settings(BaseSettings):
```

Restart QGIS after patching.

### Asset proxy returns 403

The proxy reads assets from `hv-fim-dev-data` using the EC2 instance role. A 403
means the instance role lacks read access — confirm `s3_read_paths` in
`terraform.tfvars` includes `hv-fim-dev-data` (and `hv-fim-dev-stac`). 

Verify the instance role can reach the OWP bucket from inside the proxy container:
```bash
docker exec hec-ras-stac-asset-proxy python3 -c "
import boto3
s3 = boto3.client('s3', region_name='us-east-1')
try:
    # Replace with any known key in hv-fim-dev-data
    r = s3.head_object(Bucket='fimc-data', Key='hv-fim-dev-data/hec-ras/ohio_rfc/Ohio2018a/Thumbnail.png')
    print('OK:', r['ContentLength'], 'bytes')
except Exception as e:
    print('FAILED:', e)
"
```

Note: for OWP deployments, after running `rewrite_asset_urls.py` all HREFs should point at `hv-fim-dev-data`. For NGWPC internal deployments with direct `fimc-data` access, HREFs will include `fimc-data/hv-fim-dev-data` — both are correct depending on the deployment.

### STAC Browser — WebGL map not rendering (Chrome)

If the OpenLayers map is blank in Chrome, WebGL may be disabled:

1. **Enable Hardware Acceleration:** Chrome Settings → System → turn on "Use graphics acceleration when available" → restart Chrome
2. **Enable WebGL flags:** go to `chrome://flags/#ignore-gpu-blocklist` → set "Override software rendering list" to Enabled → restart Chrome
3. Verify at `https://get.webgl.org/` — you should see a spinning cube

Alternatively, use Firefox.

### Item links point to Dewberry URLs

Item JSONs in the source catalog have `self`, `collection`, `parent`, and `root`
links pointing at `stac2.dewberryanalytics.com` — the original pipeline's API.
This is expected and does not need fixing. pgSTAC discards these links on ingest
and reconstructs them dynamically from the serving API URL. Links returned by the
OWP API will correctly point at the OWP deployment.

> **Note:** If the catalog is ever served as a static STAC catalog directly from
> S3 (without pgSTAC), these links would need to be rewritten. The STAC spec
> supports relative links (e.g. `../../collection.json`) which work regardless
> of host, but the current catalog uses absolute Dewberry API URLs. A future
> migration to static serving would require rewriting all item, collection, and
> catalog links — `rewrite_catalog_hrefs.py` is a good starting point for that.

---

## Scripts

All in `catalog-ops/`:

| Script | Purpose |
|---|---|
| `load_catalog.py` | Load STAC catalog into pgSTAC |
| `rewrite_asset_urls.py` | Rewrite S3 HREFs in pgSTAC DB to asset-proxy URLs |
| `rewrite_catalog_hrefs.py` | OWP pre-load: rewrite source bucket paths in local JSONs |
| `reset_database.sh` | Wipe DB and restart for a fresh load |
| `test_asset_proxy.sh` | End-to-end proxy smoke test |
| `diagnose_assets.sh` | Diagnose asset display issues |
| `fix_thumbnail_assets.py` | One-time thumbnail fix (already applied — do not re-run) |

For instance health checks, service restarts, and DB backups, use the
bootstrap-generated scripts at `/opt/hec-ras-stac/deployment/` (see
`deployment/Deployment_Runbook.md`).

