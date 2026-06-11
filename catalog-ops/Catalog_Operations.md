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
| Catalog scale | 158,173 items, 1,139 collections (`ble_*`, `mip_*`, `ohio_rfc`) |

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

Expected: ~159,316 objects (catalog.json, 4 program catalogs, 1,139
collection.json files, 158,173 item JSONs). Takes a few minutes.

### 1.2 Verify Sync
```bash
ls ~/hec-ras-catalog/
# Expected: catalog.json  ble/  mip/  ohio_rfc/

# Spot-check counts
find ~/hec-ras-catalog -name "collection.json" | wc -l   # 1139
find ~/hec-ras-catalog -name "*.json" \
  ! -name "catalog.json" ! -name "collection.json" | wc -l  # 158173
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

Expected output: `Layout: destination`, `Collections: 1139`, `Items: 158173`,
no "has no collection field" warnings.

### 2.2 Load

Run in `tmux` or `screen` — the full 158k load takes ~1 hour.

```bash
sudo python3 /opt/hec-ras-stac/repo/catalog-ops/load_catalog.py \
  ~/hec-ras-catalog --db-host localhost
```

### 2.3 Verify
```bash
# DB counts
docker exec -i hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "SELECT COUNT(*) FROM pgstac.collections;"   # 1139

docker exec -i hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "SELECT COUNT(*) FROM pgstac.items;"          # 158173

# Verify no unexpected collections (should return 0 rows)
docker exec -i hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "SELECT id FROM pgstac.collections
   WHERE id NOT LIKE 'ble_%'
   AND id NOT LIKE 'mip_%'
   AND id != 'ohio_rfc'
   ORDER BY id;"

# STAC API (paginated — returns 10 per page, not total)
curl -s http://localhost:8082/collections | jq '.collections | length'
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
sudo -E python3 /opt/hec-ras-stac/repo/catalog-ops/rewrite_asset_urls.py \
  --proxy-url http://${HOST_IP}:8083 \
  --db-host localhost
```

Expected: `Items updated: <N>`. Completes in seconds. Idempotent — re-running
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

### 4.1 STAC Browser Verification

Open `http://<domain>:8080` in a browser. Verify:
- Collections list renders (1,139 collections)
- Navigating into a collection shows items with geometry on the map
- Expanding an asset and clicking Download streams the file (not 403)

If the map panel is blank in Chrome, see "STAC Browser — WebGL map not
rendering" under Troubleshooting.

### 4.2 Initial Database Backup
```bash
/opt/hec-ras-stac/deployment/backup-db.sh
```

Automated weekly backups are configured (Sunday 2 AM) by the bootstrap.

---

## Rollback Plan

To redo catalog loading from a clean state, reset the database and repeat from
Phase 2:
```bash
cd /opt/hec-ras-stac/deployment
sudo bash /opt/hec-ras-stac/repo/catalog-ops/reset_database.sh --force
```

The source S3 catalog and asset data are read-only — no step here writes to S3,
so nothing on S3 needs cleanup. To tear down the infrastructure entirely, see
the Rollback Plan in `deployment/Deployment_Runbook.md`.

---

## Troubleshooting

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

### load_catalog.py — no SUMMARY printed

The loader prints per-collection progress and a SUMMARY at the end. If the
terminal disconnects mid-run, verify completion via DB counts (Phase 2.3) rather
than the log output.

### reset_database.sh fails with "no configuration file provided"

The script calls `docker-compose` without `-f` and expects to run from the
compose file's directory. Always run it from `/opt/hec-ras-stac/deployment/`:
```bash
cd /opt/hec-ras-stac/deployment
sudo bash /path/to/reset_database.sh --force
```

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
