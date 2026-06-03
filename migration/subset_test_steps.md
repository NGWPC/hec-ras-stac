# Subset Test Steps

End-to-end validation of the migration against a 10-item curated subset
(`subset_items.txt`). Exercises key classification paths from the
dump-reconciliation pre-flight: 8 items survive, 4 drop (2 hashes contain 2 items each).

## Prerequisites

- `~/ras-stac-migration/` already synced from `s3://fimc-data/dewberry-stac/`
  via `sync_items.py --whole-tree` (the drop list was built against this)
- `drop_list.txt` present in `migration/` (run
  `dump-reconciliation/build_drop_list.py` if missing — needs the local pgstac
  container from `dump-reconciliation/restore_dump.sh`; then run
  `dump-reconciliation/verify_coverage.py` to confirm exit 0)
- `.env` populated with credentials for source + destination buckets
- AWS STS tokens fresh

## Run

### Mac / Linux

```bash
cd <repo-root>/migration

# Clean slate (skip if first run)
rm -rf ~/ras-stac-migration-subset/
aws s3 rm s3://fimc-data/hv-fim-dev-stac/hec-ras-stac/ --recursive
aws s3 rm s3://fimc-data/hv-fim-dev-data/hec-ras/ --recursive

# Required env vars
export WORKING_DIR=$HOME/ras-stac-migration-subset
export ITEMS_FROM=subset_items.txt
export DATA_ROOT=s3://fimc-data/hv-fim-dev-data
export STAC_ROOT=s3://fimc-data/hv-fim-dev-stac
export STAC_API_URL=http://hec-ras-stac:8082    # any string; stac-fastapi rewrites at serve time

# Add VIA_LOCAL=1 if .env has SOURCE_AWS_* + DEST_AWS_* (dual-cred mode)

python migrate.py
python verify_subset.py 2>&1 | tee verify_subset_v<N>.log
```

### Windows (PowerShell)

```powershell
cd <repo-root>\migration

# Clean slate (skip if first run)
Remove-Item -Recurse -Force $HOME\ras-stac-migration-subset
aws s3 rm s3://fimc-data/hv-fim-dev-stac/hec-ras-stac/ --recursive
aws s3 rm s3://fimc-data/hv-fim-dev-data/hec-ras/ --recursive

# Required env vars
$env:WORKING_DIR = "$HOME\ras-stac-migration-subset"
$env:ITEMS_FROM  = "subset_items.txt"
$env:DATA_ROOT   = "s3://fimc-data/hv-fim-dev-data"
$env:STAC_ROOT   = "s3://fimc-data/hv-fim-dev-stac"
$env:STAC_API_URL = "http://hec-ras-stac:8082"    # any string; stac-fastapi rewrites at serve time

# Add $env:VIA_LOCAL = "1" if .env has SOURCE_AWS_* + DEST_AWS_* (dual-cred mode)

python migrate.py
python verify_subset.py 2>&1 | Tee-Object verify_subset.log
```

## Expected output

| Component | Count |
|---|---:|
| Items pulled by `sync_items.py` (Phase 1) | 10 |
| Items skipped by drop list (Phase 4) | 4 |
| Items at destination | 8 |
| STAC bucket objects | 19 (1 root + 3 program catalogs + 7 collection.json + 8 item JSONs) |
| Data bucket item dirs | 8 |
| `verify_subset.py` asset-HREF resolution | **0 missed** |

Program-catalog breakdown for the subset:
- `ble/catalog.json` — 2 collections (`ble_11110105_Poteau`, `ble_12100201_UpperGuadalupe`)
- `mip/catalog.json` — 3 collections (`mip_03050110`, `mip_11090204`, `mip_no_crs`)
- `ohio_rfc/catalog.json` — 1 collection (`ohio_rfc`) — bucket D, collection assigned via override

The 6 surviving items:

| Item (destination id) | Collection | Bucket | Note |
|---|---|---|---|
| `UNT_077_in_LPR` | `ble_11110105_Poteau` | A | exact match |
| `SPRING_CREEK_1` | `ble_12100201_UpperGuadalupe` | A | id rewritten from `SPRING_CREEK` (dump applied `_1` suffix) |
| `McKenzie_Creek_Tributary_2` | `mip_03050110` | A | |
| `Tributary_AA` | `mip_11090204` | A | |
| `DEEP_FORK_TRIBUTARY` | `mip_no_crs` | A | |
| `Ohio2018a` | `ohio_rfc` | D | uncatalogued locally; collection assigned via `collection_overrides.tsv` |

The 4 dropped items exercise: bucket C (catalogued but absent from dump),
bucket E (uncatalogued superseded), bucket F (uncatalogued orphan),
bucket B2a (mip_no_crs corrected by dump).

## EC2 load + browser

After `verify_subset.py` shows 0 misses, optionally load into pgSTAC and
sanity-check via STAC Browser:

```bash
# On the EC2 instance (or local docker compose for the stack)
mkdir -p ~/load_subset
aws s3 sync s3://fimc-data/hv-fim-dev-stac/hec-ras-stac/ ~/load_subset/

python3 /path/to/runtime/load_catalog.py ~/load_subset --db-host localhost --dry-run
python3 /path/to/runtime/load_catalog.py ~/load_subset --db-host localhost

# Verify pgSTAC
docker exec hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "SELECT collection, count(*) FROM pgstac.items GROUP BY collection ORDER BY collection;"
# Expect 6 collections, 1 item each (ble×2, mip×3, ohio_rfc×1)

# Optional: rewrite asset HREFs for STAC Browser preview
export HOST_IP=$(hostname -I | awk '{print $1}')
export PGPASSWORD=$(sudo cat /opt/hec-ras-stac/.db_password)
python3 /path/to/runtime/rewrite_asset_urls.py --proxy-url http://${HOST_IP}:8083 --db-host localhost
```

Open `http://<ec2-host>:8080` in a browser — 6 collections navigable, each
with 1 item.

## Cross-account mode

If source and destination are in different accounts, set
`SOURCE_AWS_*` + `DEST_AWS_*` in `.env` and add `VIA_LOCAL=1`. Phase 6
(`sync_assets.py`) will stage each item through the laptop. Subset only —
do not use at full scale.

## Troubleshooting

- **`generate_collections.py` emits an empty collection at the
  destination** — local working dir has stale items from a prior run.
  Delete `$WORKING_DIR` and re-run.
- **`aws s3 sync` leaves stale objects at the destination** — the
  destination is not purged before re-upload. Use the `aws s3 rm`
  commands at the top before re-running.
- **`verify_subset.py` reports MISSED HREFs** — STS tokens may have aged
  out, or `rewrite_hrefs.py` didn't restructure items into destination
  shape. Check `Items checked: 8` first; if 0, Phase 4 failed.
