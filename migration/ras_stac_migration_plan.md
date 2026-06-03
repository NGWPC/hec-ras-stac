# HEC-RAS STAC Migration Plan

Migrates the Dewberry HEC-RAS STAC catalog into a target AWS account, backed
by the same pgSTAC + stac-fastapi + STAC Browser + asset-proxy stack used for
BenchmarkCat. The migration scripts are in this directory; the EC2 stack is
stood up by the Terraform tree in `../deployment/terraform/`.

For per-decision rationale (suffix scheme, deferred FEMA rename, etc.) see the
analysis files in this directory. For the operator walkthrough with verification
commands, see **`subset_test_steps.md`**.

## Overview

| Component | Detail |
|---|---|
| Source | `s3://fimc-data/dewberry-stac/` (item HREFs use the legacy alias `s3://fim/`) |
| Source prefixes | `ebfe`, `mip_30`, `mip_70` (storage prefixes, **not** collection IDs) |
| Target STAC bucket | metadata JSONs only — `s3://<stac-bucket>/hec-ras-stac/` |
| Target data bucket | all assets — `s3://<data-bucket>/hec-ras/` |
| EC2 services | PostgreSQL/pgSTAC (5432), STAC API (8082), STAC Browser (8080), asset-proxy (8083) |
| Catalog scale | ~178k items, 1,140 source collections |

The catalog is migrated in two parts: **metadata** (catalog.json, collection
JSONs, item JSONs) goes to the STAC bucket; **assets** (model files, gpkgs,
thumbnails) go to the data bucket. Items are organized by their collection at
the destination — the source's `<prefix>/<hash>/` shape is a provenance
artifact and is dropped.

Migration scope is determined by reconciliation against Dewberry's pgstac
dump (the published source of truth). Local items in the S3 export that
match an entry in the dump on `(id, collection, source_hash)` migrate;
items not in the dump are dropped. 158,178 of the 178,826 local items
survive the reconciliation. See `dump-reconciliation/README.md`
for the full classification rules.

Each local item is assigned to one of eight buckets:

| Bucket | Decision | Description | Output file |
|---|---|---|---|
| **A** | MIGRATE | Exact match — `(id, collection, hash)` aligns with dump | `id_rewrites.tsv` (if dump applied `_N` suffix) |
| **B1** | DROP | Same id, different collection, e_tags match — same physical model published elsewhere | `drop_list.txt` |
| **B2a** | DROP | Local `mip_no_crs`, dump has it under a real collection with different files — dump's corrected version supersedes | `drop_list.txt` |
| **B2b** | MIGRATE | Same id, different real collection, no e_tag overlap — independent models sharing a name (id unique within collection, no destination collision) | — |
| **C** | DROP | Local catalogued, id absent from dump entirely | `drop_list.txt` |
| **D** | MIGRATE | Local uncatalogued (`collection: null`), exact `(id, hash)` match in dump — migrates with collection override | `collection_overrides.tsv` |
| **E** | DROP | Local uncatalogued, id in dump under a different hash — superseded version | `drop_list.txt` |
| **F** | DROP | Local uncatalogued, id absent from dump entirely — genuine orphan | `drop_list.txt` |

The destination catalog has no `hec_ras_uncatalogued` collection — every
surviving item has a real `collection` value matching Dewberry's
published catalog.

## Target Layout

Per-program parent Catalog above the Collections — `ble/`, `mip/`, `ohio_rfc/` — so the 1,140-collection tree is navigable.

```
s3://<stac-bucket>/
└── hec-ras-stac/
    ├── catalog.json
    ├── ble/
    │   ├── catalog.json
    │   ├── <collection>/
    │   │   ├── collection.json
    │   │   └── <item-id>/<item-id>.json
    │   └── ... (113 ble_* collections)
    ├── mip/
    │   ├── catalog.json
    │   ├── <collection>/
    │   │   ├── collection.json
    │   │   └── <item-id>/<item-id>.json
    │   └── ... (1,026 mip_* collections)
    └── ohio_rfc/
        ├── catalog.json
        └── ohio_rfc/
            ├── collection.json
            └── <item-id>/<item-id>.json

s3://<data-bucket>/hec-ras/
└── <collection-id>/<item-id>/
    ├── thumbnail.png                         (where present in source)
    ├── <item-id>.gpkg                        (where present in source)
    └── ... (model files flat; MODELING/ subdir preserved verbatim where present)
```

Classification rule: `ble_*` → `ble/`, `mip_*` → `mip/`, `ohio_rfc` → `ohio_rfc/`.
program catalogs are kept as scaffolds so consumers know the slots exist.

The data bucket stays flat — assets aren't browsed by humans, ripple1d reads
`s3_key` directly via boto3, and a parent layer there would only add
indirection. pgstac is unaffected by the catalog tree shape (it flattens to
`(collection_id, item_id)` regardless).

See `S3_organization.txt` for the full diagram including HREF rewrite rules.

## HREF Transformation

Item JSON asset HREFs are rewritten; everything else in the item is preserved
as-is, except:

- Provenance properties added on every item: `original_source_hash`,
  `original_source_hash_dir`, `original_source_prefix`. Safe to drop
  post-migration once the legacy Dewberry bucket is decommissioned.

Items dropped by the `dump-reconciliation/` pre-flight (anything not in
Dewberry's pgstac dump) are skipped during the per-item walk and never
reach this rewrite step.

| Source HREF (matched) | Destination HREF (written) |
|---|---|
| `s3://fim/<prefix>/source_models/<hash>/<file>` | `s3://<data-bucket>/hec-ras/<collection-id>/<item-id>/<file>` |
| `s3://fim/<prefix>/stac_items/<hash>/<file>` | `s3://<data-bucket>/hec-ras/<collection-id>/<item-id>/<file>` |
| `https://fim.s3.amazonaws.com//<prefix>/stac_items/<hash>/<file>` | `s3://<data-bucket>/hec-ras/<collection-id>/<item-id>/<file>` |

Both source sections (`source_models/` and `stac_items/`) flatten into the
same per-item destination directory. Verified across the full source: zero
filename collisions between the two sections within any hash.

Each rewritten asset also gets an `s3_key` field — the full bucket-relative
key — so `ripple1d-pipeline` can read it directly via boto3.

Item `links` arrays are **not** rewritten. stac-fastapi regenerates `self`,
`collection`, `parent`, `root` links at serve time from the request host, so
the durable S3 copy carries the source links for provenance.

## Migration Scripts

The codebase splits into two top-level subtrees:

- **`migration/`** — one-shot migration pipeline.
- **`runtime/`** — durable EC2-side operational tooling (sits alongside
  `deployment/`).

### Migration pipeline (`migration/`)

| Script | Role |
|---|---|
| `sync_items.py` | Pull item JSONs from Dewberry → local `items/<source_prefix>/<source_hash_dir>/<item-id>.json`. Three modes: per-hash (`--subset N`), whole-tree (full-scale), `--items-from FILE` (curated subset). |
| `generate_collections.py` | Fetch collections from Dewberry API → local `collections/`. Default: filter to collections actually referenced by local items (after the drop-list pre-flight, this is what makes it to the destination). Pass `--all` to emit every API collection. |
| `generate_catalog.py` | Write the top-level `catalog.json` plus one program-level `catalog.json` per `ble/`/`mip/`/`ohio_rfc/`. Root catalog links to program catalogs; program catalogs link to their Collections. |
| `rewrite_hrefs.py` | Load drop list from `drop_list.txt` and skip matching items. For surviving items: rewrite asset HREFs, restructure items source → destination layout, attach `original_source_*` provenance. |
| `upload_to_s3.py` | Per-collection walk; route each Collection upload into its program dir under `<stac-root>/hec-ras-stac/<program>/<collection>/...` per the classification rule. |
| `sync_assets.py` | Per-item S3-to-S3 copy of source assets → data bucket (flat). Reads provenance props set by `rewrite_hrefs.py`. `--via-local` for cross-account staging. |
| `migrate.py` | Orchestrator — runs the six phases above in order. Reads required env vars `DATA_ROOT`, `STAC_ROOT` (full S3 URIs — either `s3://<bucket>` or `s3://<bucket>/<prefix>`), `STAC_API_URL`, plus optional `WHOLE_TREE=1`, `SUBSET=N`, `ITEMS_FROM=<file>`, `DRY_RUN=1`, `VIA_LOCAL=1`. Auto-loads `../.env` and detects single-cred vs dual-cred mode. |
| `verify_subset.py` | Verification harness for subset runs. Auto-promotes `DEST_AWS_*` → `AWS_*` in dual-cred mode. |
| `verify_migration.py` | Verification harness for full-scale runs. Checks STAC bucket counts, link-chain, per-bucket classification spot-checks (survivors present, drops absent), sampled asset HREF resolution, and asset sync log summary. |
| `dump-reconciliation/` | Drop-list pre-flight: reconciles the local S3 export against Dewberry's pgstac dump (the source of truth) and emits the list of local items to skip during migration. |

### EC2-side operational tooling (`runtime/`)

| Script | Role |
|---|---|
| `load_catalog.py` | Load `catalog.json` + collections + items into pgSTAC. Auto-detects two input layouts: working-dir (post-`migrate.py`) or flat-destination (post-`aws s3 sync` from S3). |
| `rewrite_asset_urls.py` | Post-load: rewrite `s3://` HREFs in pgSTAC to asset-proxy URLs (so STAC Browser renders assets). Leaves `s3_key` untouched (ripple1d reads it directly). |
| `reset_database.sh`, `test_asset_proxy.sh`, `diagnose_assets.sh` | EC2 utilities (DB reset, proxy smoke test, diagnostics). |
| `requirements.txt` | `psycopg2-binary` — only needed by `load_catalog.py` and `rewrite_asset_urls.py`. |

---

## Phase 0 — Prerequisites

- AWS access: read on `s3://fimc-data/dewberry-stac/*`; write on the target STAC and data buckets. 
- Target buckets created.
- For the EC2 phases: Terraform applied (`../deployment/terraform/`), the
  four containers up, and `pip3 install psycopg2-binary` available on the
  instance.

**Gate:** confirm source read access before proceeding —
`aws s3 ls s3://fimc-data/dewberry-stac/ | head`.

## Phase 1 — Subset Validation (do this first)

Run a small reproducible slice end-to-end before the full migration. The
canonical subset is the 10-item curated list at `subset_items.txt` — it
exercises key classification paths from the drop-list pre-flight
(A / A-with-id-rewrite / B2a / C / D / E / F, including ohio_rfc program catalog).
6 items land at the destination after the drop list filters out superseded / orphan items.

Full procedure with verification steps after each phase:
**`subset_test_steps.md`**.

In short:
```bash
export DATA_ROOT=s3://<bucket>/<prefix-or-none>
export STAC_ROOT=s3://<bucket>/<prefix-or-none>
export STAC_API_URL=http://<stac-host>:8082
export ITEMS_FROM=subset_items.txt
python migrate.py # add VIA_LOCAL=1 if dual-cred
python verify_subset.py 2>&1 | tee verify_subset.log
```

`verify_subset.py` runs the full verification sweep: S3 destination listings,
per-item asset checks, and the HREF resolution loop. Expected end of log:
```
Items checked:      8
Asset HREFs hit:    70
Asset HREFs MISSED: 0
```

**Gate:** subset HREF resolution shows 0 misses before the full migration.

## Phase 2 — Full Migration to Target S3

Same flow, no subset, single-cred mode, direct S3-to-S3:
```bash
rm -rf ~/ras-stac-migration                    # clean slate
unset SUBSET ITEMS_FROM VIA_LOCAL
export DATA_ROOT=s3://<data-bucket>
export STAC_ROOT=s3://<stac-bucket>
export STAC_API_URL=http://<stac-host>:8082
python migrate.py
```

Notes:
- The asset sync (Phase 6 of `migrate.py`) is the long pole — hours at full
  scale. Run in `tmux` / `screen` (Linux) or keep the RDP session alive
  (Windows — use a mouse-jiggler script to prevent idle disconnect).
- All `aws s3 cp` / `aws s3 sync` operations are idempotent — safe to re-run on partial failure.

Verify against S3:
```bash
aws s3 cp s3://<stac-bucket>/hec-ras-stac/catalog.json - | jq '.id'        # "hec-ras-stac"
aws s3 ls s3://<stac-bucket>/hec-ras-stac/ | head                          # program dirs + catalog.json
aws s3 ls s3://<stac-bucket>/hec-ras-stac/mip/ | head                      # mip_* collections
aws s3 ls s3://<data-bucket>/hec-ras/ --recursive | wc -l                  # large
```

## Phase 3 — Load into pgSTAC

On the EC2, get the catalog onto the instance:

**Pull directly from S3** (recommended — no working-dir transfer):
```bash
mkdir -p ~/load_full
aws s3 sync s3://<stac-bucket>/hec-ras-stac/ ~/load_full/
python3 /path/to/runtime/load_catalog.py ~/load_full --db-host localhost --dry-run
python3 /path/to/runtime/load_catalog.py ~/load_full --db-host localhost
```

`load_catalog.py` auto-detects which layout it's looking at. It upserts every
collection, then walks the items, groups by each item's `collection` field,
and batch-upserts per group. The DB password is read from `--db-password`,
`PGPASSWORD`, or `/opt/hec-ras-stac/.db_password`.

Verify:
```bash
docker exec -i hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "SELECT collection, COUNT(*) FROM pgstac.items GROUP BY collection ORDER BY collection;"
curl http://<stac-host>:8082/collections | jq '.collections | length'
curl "http://<stac-host>:8082/collections/<a-collection-id>/items?limit=1" | jq '.features[0].id'
```

**Rollback:** `runtime/reset_database.sh --force` (EC2), then re-run
`runtime/load_catalog.py`.

## Phase 4 — Asset URL Rewriting (for STAC browser)

Run only if STAC Browser needs to render thumbnails / stream assets. This
rewrites the `s3://` asset HREFs **in pgSTAC** to route through the
asset-proxy (port 8083). The `s3_key` field is intentionally left untouched
because `ripple1d-pipeline` reads `s3_key` directly via boto3 for the real
key.

> **Before full-catalog use:** `rewrite_asset_urls.py:_get_items()` does
> `fetchall()` on the whole `pgstac.items` table — ~1-2 GB in memory at
> 178k items. Switch to a server-side cursor before running unscoped at
> full scale. Always-safe path: pass `--collection-prefix`.

```bash
export HOST_IP=$(hostname -I | awk '{print $1}')
export PGPASSWORD=$(sudo cat /opt/hec-ras-stac/.db_password)
python3 /path/to/runtime/rewrite_asset_urls.py --proxy-url http://${HOST_IP}:8083 --db-host localhost --dry-run
python3 /path/to/runtime/rewrite_asset_urls.py --proxy-url http://${HOST_IP}:8083 --db-host localhost
```

Smoke test: `runtime/test_asset_proxy.sh http://localhost:8083`. Diagnostics:
`runtime/diagnose_assets.sh`.

## Phase 5 — Downstream

`ripple1d-pipeline` connects via `RP_STAC_URL`. Update its `.env`:
```
RP_STAC_URL=http://<stac-host>:8082
```

No ripple1d code changes — the pipeline queries by collection ID
(unchanged) and reads `asset.s3_key` for the `ras-geometry-gpkg` role,
which now points at the target data bucket transparently.

---

## Downstream Compatibility Notes

`ripple1d-pipeline` (`src/setup/stac_importer.py`):

1. `client.get_collection(collection_id)` — collection IDs unchanged → no impact
2. `collection.get_items()` — item IDs mirror Dewberry's published view (pg dump).
   454 items receive a `_N` numeric suffix via `id_rewrites.tsv` (within-collection
   deduplication applied by Dewberry's dump); all other IDs are unchanged.
3. Reads `asset.s3_key` for role `ras-geometry-gpkg` — rewritten to the
   target data bucket → transparent.
4. `bucket, key = href.replace("s3://", "").split("/", 1)` — works with the
   new bucket name.

Only change required downstream: `RP_STAC_URL` in `.env`.
