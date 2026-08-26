# Integrating New State/Program Models into the HEC-RAS STAC Catalog

## Overview

This doc describes the general process for onboarding a new batch of HEC-RAS
STAC data (a new state, program, or vendor delivery) into the authoritative
catalog documented in [`catalog-ops/Catalog_Operations.md`](Catalog_Operations.md)
and [`deployment/Deployment_Runbook.md`](../deployment/Deployment_Runbook.md).
Those two docs remain the delivery-readiness gold standard; this doc covers the
integration-specific pieces: verifying a new delivery's structure, rewriting it
to match the authoritative schema, and rolling it out via a pilot before a full
transfer.

The second half of this doc ([NC/MN Integration Reference](#nc-mn-integration-reference))
walks through a completed, real-world run of this process — the North Carolina
and Minnesota (NC/MN) model integration — with the actual commands used, kept
for reference when running this process again.

**Three phases:**
1. **Verify** the new delivery's structure against the authoritative schema.
2. **Pilot** a small subset of collections end-to-end (generate → upload → load →
   proxy-rewrite → downstream pipeline run) before committing to the full transfer.
3. **Full rollout** — same pipeline, full scale.

---

## Phase 1 — Structural verification

Before touching the authoritative catalog, scan the new delivery and compare it
against a known-good reference collection/item already in the authoritative
catalog. The goal is to classify every structural divergence as either "trivial,
fix in the rewrite step" or "needs a follow-up with the data provider."

Script: `catalog-ops/verify_nc_mn_structure.py` (read-only, re-runnable — the
name reflects its origin but it works generically against any new delivery
directory):
```bash
python3 catalog-ops/verify_nc_mn_structure.py \
  --delivery-dir <path-to-new-delivery> \
  --reference-collection <path-to-known-good-collection.json> \
  --reference-item <path-to-known-good-item.json>            # summary

python3 catalog-ops/verify_nc_mn_structure.py ... --show-all  # full per-offender listing
python3 catalog-ops/verify_nc_mn_structure.py ... --json-report report.json  # machine-readable
```

Typical categories of divergence to expect from a new vendor delivery:
- Asset href scheme pointing at the vendor's own staging location instead of
  the destination data root.
- Missing `s3_key` (a bucket-relative asset key some downstream consumers read
  directly via boto3, rather than parsing the `href`).
- Inconsistent thumbnail asset naming/casing or media type placement.
- Older STAC extension versions (e.g. projection extension `v1.1.0` vs. the
  authoritative catalog's `v2.0.0`).
- Missing collection-level fields (`title`, correct `license` value) that the
  authoritative schema expects but the vendor's delivery omits.

Each divergence type should be checked against the *authoritative* catalog's
own precedent (not assumed) before deciding whether it needs a fix — some
"divergences" turn out to already be common/expected patterns in the existing
catalog (e.g. a percentage of items with null CRS or version fields), and don't
need special handling.

### Extension-version decisions

When a vendor delivery uses an older or inconsistent STAC extension version
than the authoritative catalog, check:
1. What version(s) the catalogs you already operate use — consistency across
   catalogs is worth more than matching the vendor's original version.
2. What any adjacent/related ingest pipelines already write, if applicable.
3. Whether anything downstream actually reads the field in a
   version-sensitive way — if not, it's a presentation-consistency call with
   no functional risk.

Document the decision and any resulting fix in the rewrite script (see Phase 2).

---

## Phase 2 — Pilot integration

**Goal:** prove the full pipeline — regenerate → upload → load → proxy-rewrite
→ downstream consumer run — on a small subset of collections before committing
to the full transfer. This loads directly into **production** S3/pgSTAC, so
pick collection IDs with a namespace/prefix that can't collide with anything
already in the catalog.

**Constraint to plan around:** the existing catalog-loading scripts
(`upload_to_s3.py`, `generate_catalog.py`, `sync_items.py`, `sync_assets.py`, if
present in your version of this repo) generally operate on a whole
`--working-dir` tree rather than accepting a per-collection filter flag.
Scoping to a pilot subset means building a small scratch working directory
containing only the pilot collections, rather than passing a filter flag.

### Working-directory discipline

Treat any local mirror of the vendor delivery or the authoritative catalog as
**pristine, pull-only** — never edit in place. Do all generation work in a
dedicated scratch directory, which can always be deleted and rebuilt from the
mirrors — this is what makes iterating on a broken pilot run free.

### 2.1 Pilot selection

Pick a small number of the smallest collections by item count (fastest
iteration), representative of the delivery's most common shape (e.g. if most
collections are single-item, include some single-item ones — don't only pick
the largest/most complex as your first test).

### 2.2 Stage the pilot subset

Copy just the selected collections from the read-only delivery mirror into the
scratch working directory's raw-input location.

### 2.3 Regenerate

Write (or reuse/extend) a generation script that:
- Reads the vendor's delivery shape (whatever `collection.json`/item JSON
  layout they ship).
- Writes the shape your upload tooling expects (root `catalog.json`, per-program
  catalogs, per-collection `collection.json`, per-item item JSON).
- Applies every fix identified in Phase 1's structural verification.

Make it idempotent and support a `--dry-run` and a `--collections <id,...>`
filter, so it's reusable for both the pilot and the full rollout without
duplicating logic.

Validate the output two ways: (1) against the Phase 1 divergence table — every
fix should be visible in the regenerated output; (2) byte-for-byte structural
comparison against a real authoritative item/collection — same top-level keys,
same `stac_extensions`, same per-asset field shapes.

If your upload tooling routes items by ID prefix into per-program subdirectories
(a "PROGRAMS" list or equivalent), add the new prefix(es) there too.

### 2.4 Upload pilot data to S3

**Metadata**: dry-run your upload script first, review, then run for real. This
should be additive only — new program/collection keys added, nothing existing
overwritten except a shared root `catalog.json` (if your catalog structure has
one), which should only gain new child links.

**Assets**: if source and destination live in the same bucket (different
prefix), a same-bucket `aws s3 sync` per collection avoids a local
download/upload round-trip. Dry-run first.

⚠️ **Don't skip the asset sync.** If metadata goes live with hrefs pointing at
asset keys that don't exist yet, downstream consumers will see 404s. Confirm
the asset sync actually completed before trusting any pilot collection's asset
downloads.

Before the first real upload, snapshot anything you're about to overwrite (e.g.
the root `catalog.json`) so there's a fast rollback path.

### 2.5 Rollback reference

Because a pilot should only ever *add* new, non-overlapping collection IDs,
every rollback should be a scoped delete + redo — never a full re-sync or DB
reset:

- **Scratch dir wrong, not uploaded yet:** delete and redo staging + generation.
  Free, no AWS calls.
- **Uploaded metadata/assets wrong:** scoped `aws s3 rm --recursive` on just the
  new collection prefixes, in both the metadata and data buckets.
- **DB load wrong:** use a scoped `DELETE FROM pgstac.collections WHERE id LIKE
  '<new-prefix>_%'` (cascades to items) rather than a full database reset — a
  full reset takes the live API offline for everyone and drops every existing
  collection, not just the new ones. Take a full-DB backup before the first
  load as a safety net regardless.
- **Proxy URL rewrite wrong:** re-running the catalog load step generally
  re-upserts the original `s3://` hrefs from S3, undoing a bad proxy rewrite —
  no row deletion needed.

### 2.6 Load into pgSTAC + rewrite proxy URLs

Follow the existing `Catalog_Operations.md` load + proxy-rewrite phases,
scoped to just the new program/collection prefixes (not a full catalog
re-sync). If the load tooling has a hardcoded batch/prefix list for existing
programs, check whether it needs the new prefix(es) added explicitly, or
whether it needs to be invoked per-prefix instead.

Before the load: check any pre-existing safety thresholds a prior migration
required (e.g. database lock limits ahead of a bulk load) — confirm the
current value rather than assuming it still holds, since anything requiring a
service restart briefly takes a live API offline for all consumers, not just
this integration.

Verify: collection/item counts match expectations, no raw (unrewritten) asset
URLs remain, and spot-check that assets actually download (not 403) for a
couple of pilot items via both the API and the catalog browser UI.

### 2.7 End-to-end test via the downstream consumer

Whatever consumes this catalog downstream (e.g. ripple1d-pipeline)
should be pointed at the updated catalog and run against just the pilot
collections. A clean run through that pipeline is strong confirmation that:
- Asset hrefs, bucket-relative keys, and any asset-proxy layer are all correct
  (the pipeline can actually pull/download the model files).
- The STAC item's `properties` schema is compatible with what the pipeline
  expects.
- Whatever output artifact the pipeline produces (e.g. a processed model
  library) is generated correctly.

Iterate on any failures: if it traces back to a STAC-structure issue, fix the
generation script and redo the affected collection (scoped rollback → redo);
if it's an issue in the downstream pipeline itself unrelated to the STAC data,
track it separately rather than blocking the rest of the pilot on it.

**Exit criteria:** all pilot collections load, browse, and complete a full
downstream pipeline run cleanly. Any script/logic changes made to get here are
then considered validated for the full rollout.

---

## Phase 3 — Full rollout

Same pipeline as the pilot, at full scale — this is execution, not design.
Use the same working-directory discipline as the pilot, in a separate scratch
directory.

1. **Regenerate**, unfiltered (should be idempotent — re-including the pilot
   collections is harmless).
2. **Metadata upload** — dry-run first, same tooling as the pilot.
3. **Asset transfer** — same per-collection sync loop as the pilot, looped over
   every new collection. This is typically the longest step (can be
   multi-hour at full scale) — run it somewhere that survives a disconnected
   session. It should be safely restartable: each collection's sync is
   independent, so re-running is a cheap no-op for already-synced ones.
4. **Verify asset coverage** — a full, unsampled diff of "what should exist"
   (every asset referenced by the regenerated items) against "what actually
   exists" in the destination data bucket, for a zero-gap confirmation before
   moving to the DB load.
5. **Full DB load** — same as the pilot's load step, at full scale. Take a
   full-DB backup first; re-verify counts before considering it committed (a
   `--dry-run` that worked at pilot scale is not a guarantee it'll behave
   identically at full scale). Re-check any safety thresholds (e.g. lock
   limits) before this step specifically — a small pilot may never trigger a
   limit that a much larger load will, and the fix for that may require a
   brief service restart. Plan for that possibility even if the pilot never
   hit it.
6. **Update repo-wide counts** — every doc stating the previous catalog scale
   (item/collection counts) needs the new totals.
7. **Full downstream pipeline run** across all new collections.
8. **Final sign-off** against your deployment runbook's go-live checklist, at
   full scale.

**Rollback at full scale**: same scoped-delete approach as the pilot, just
bigger — budget more time, not an instant operation, for a full-scale scoped
delete.

---

## Open items / lessons for the next integration

- Watch for STAC extension version mismatches between different object types
  in the same catalog (e.g. Catalog objects vs. Collection/Item objects) —
  these can be pre-existing and intentionally left as-is rather than "fixed,"
  if changing them isn't actually required for correctness.
- Don't assume catalog-load idempotency is risk-free on a mixed old+new load
  just because it worked at pilot scale — re-verify via `--dry-run` counts
  before every load.
- Keep bucket-naming/config docs in sync with what's actually live in the
  target account — drift here is a common source of confusion for whoever
  runs this process next (`s3://fimc-data/`). 

---

## NC/MN Integration Reference

The following is the record of a completed run of this process — integrating a
new delivery of North Carolina (NC) and Minnesota (MN) HEC-RAS flood models
into the authoritative catalog. Kept for reference: the actual commands,
decisions, and gotchas hit along the way.

### Background

A new batch of HEC-RAS STAC data for NC and MN flood models was delivered to a
staging area on S3:

- `s3://fimc-data/dewberry-stac/hv-fim-dev-data/` (assets)
- `s3://fimc-data/dewberry-stac/hv-fim-dev-stac/` (STAC metadata)

Synced locally for inspection (read-only mirrors):

- `~/Desktop/work/dewberry-stac` ← new NC/MN delivery
- `~/Desktop/work/hec-ras-stac` ← current authoritative catalog, synced from
  `s3://fimc-data/hv-fim-dev-stac/hec-ras-stac/`

This integration landed NC/MN into the same authoritative catalog documented in
`Catalog_Operations.md` and `Deployment_Runbook.md` — prior to this
integration, **158,173 items / 1,139 collections** (`ble_*`, `mip_*`,
`ohio_rfc`) served from one EC2 running 4 Docker containers (pgSTAC DB,
stac-fastapi API, STAC Browser, asset-proxy).

**Bucket naming** — both the STAC metadata bucket and the data bucket live
under the `fimc-data` prefix in this account:

- STAC metadata root: `s3://fimc-data/hv-fim-dev-stac/hec-ras-stac/`
- Data root: `s3://fimc-data/hv-fim-dev-data/hec-ras/`

### Phase 1 result

Scanned the full delivery: 292 collections, 8,434 items. 15 divergence types
found, all systematic (100% of items/collections), all mechanical, zero
needing a follow-up with the data provider.

**Fixes applied in the rewrite** (`catalog-ops/generate_nc_mn_catalog.py`):

| Field | Delivered as | Rewritten to |
|---|---|---|
| Asset href scheme | vendor staging prefix | destination data root |
| Asset `s3_key` | Absent | Present, bucket-relative |
| Thumbnail asset key/`title` | Inconsistent casing | `"thumbnail"` (key + title), lowercase |
| Thumbnail media type | Stuffed into `roles` | Proper `type` field |
| Projection extension | v1.1.0, `proj:epsg` (int) | v2.0.0, `proj:code` (string) |
| Item `fim_group` | Absent | Derived from collection `summaries.fim_groups` |
| Collection `title` | Absent | `<collection-id>` |
| Collection `license` | `"other"` | `"proprietary"` |

**Verified as not needing action** (checked against the authoritative catalog,
not assumed):
- A meaningful share of items with null `ras_version`/`proj:epsg` — the
  authoritative catalog has the same pattern at an equal or higher rate.
- A vendor-specific extra field on flow assets with no authoritative
  precedent — kept as harmless extra provenance.
- Item `links` pointing at the vendor's own S3 location — matches existing
  precedent (the API regenerates `self`/`collection`/`parent`/`root` links at
  serve time; the durable copy keeps source links for provenance).
- Item IDs appearing in more than one collection — expected, matches existing
  precedent.

### Pilot selection

3 smallest NC + 3 smallest MN collections by item count (fastest iteration),
excluding catch-all `_Other` collections. All 6 were single-item collections —
the common case for this NC delivery, not an edge case.

```bash
export PILOT_MN="mn_27041 mn_27053 mn_27073"
export PILOT_NC="nc_370652011071501 nc_371040200912221 nc_371040201202141"
```

### Staging + regeneration

```bash
mkdir -p ~/Desktop/work/nc-mn-migration-work/pilot/raw
for cid in $PILOT_MN; do cp -R ~/Desktop/work/dewberry-stac/mn/$cid ~/Desktop/work/nc-mn-migration-work/pilot/raw/$cid; done
for cid in $PILOT_NC; do cp -R ~/Desktop/work/dewberry-stac/nc/$cid ~/Desktop/work/nc-mn-migration-work/pilot/raw/$cid; done

python3 catalog-ops/generate_nc_mn_catalog.py \
  --raw-dir ~/Desktop/work/nc-mn-migration-work/pilot/raw \
  --out-dir ~/Desktop/work/nc-mn-migration-work/pilot \
  --data-root s3://fimc-data/hv-fim-dev-data \
  --reference-root-catalog ~/Desktop/work/hec-ras-stac/catalog.json
```

To regenerate from scratch after editing the script:
```bash
rm -rf ~/Desktop/work/nc-mn-migration-work/pilot/{collections,items,program_catalogs,catalog.json}
python3 catalog-ops/generate_nc_mn_catalog.py \
  --raw-dir ~/Desktop/work/nc-mn-migration-work/pilot/raw \
  --out-dir ~/Desktop/work/nc-mn-migration-work/pilot \
  --data-root s3://fimc-data/hv-fim-dev-data \
  --reference-root-catalog ~/Desktop/work/hec-ras-stac/catalog.json
```

`("mn_", "mn")` / `("nc_", "nc")` were added to the `PROGRAMS` list in
`migration-archive/generate_catalog.py` and `migration-archive/upload_to_s3.py`
so the upload step routes `mn_*`/`nc_*` collections to the right program
subdirectory.

### Uploading the pilot

**Metadata**:
```bash
python3 migration-archive/upload_to_s3.py \
  --working-dir ~/Desktop/work/nc-mn-migration-work/pilot \
  --stac-root s3://fimc-data/hv-fim-dev-stac \
  --dry-run
# review, then:
python3 migration-archive/upload_to_s3.py \
  --working-dir ~/Desktop/work/nc-mn-migration-work/pilot \
  --stac-root s3://fimc-data/hv-fim-dev-stac
```

**Assets** (same-bucket copy, different prefix — no local download/upload
round-trip):
```bash
# dry run first
for cid in $PILOT_MN $PILOT_NC; do
  aws s3 sync "s3://fimc-data/dewberry-stac/hv-fim-dev-data/$cid/" "s3://fimc-data/hv-fim-dev-data/hec-ras/$cid/" --dryrun
done
# review, then run for real
for cid in $PILOT_MN $PILOT_NC; do
  aws s3 sync "s3://fimc-data/dewberry-stac/hv-fim-dev-data/$cid/" "s3://fimc-data/hv-fim-dev-data/hec-ras/$cid/"
done
```

**Lesson learned**: the asset sync step was skipped during the first pass at
this pilot — metadata went live with hrefs pointing at asset keys that didn't
exist yet, causing 404s in the downstream pipeline. Confirm the asset sync
actually ran before trusting any collection's asset downloads.

### Rollback commands used for reference

```bash
for cid in $PILOT_MN $PILOT_NC; do
  prog=$(echo $cid | cut -d_ -f1)
  aws s3 rm --recursive "s3://fimc-data/hv-fim-dev-stac/hec-ras-stac/$prog/$cid/"
  aws s3 rm --recursive "s3://fimc-data/hv-fim-dev-data/hec-ras/$cid/"
done
```
Never touched `ble`/`mip`/`ohio_rfc`.

Scoped DB delete, if needed:
```sql
DELETE FROM pgstac.collections WHERE id LIKE 'mn\_%' OR id LIKE 'nc\_%';
```
(cascades to items)

### Loading into pgSTAC + proxy URL rewrite (**On EC2 Serving the HEC-RAS-STAC**)

```bash
# On EC2, from /opt/hec-ras-stac/repo/catalog-ops/
mkdir -p ~/hec-ras-catalog-pilot
aws s3 cp s3://fimc-data/hv-fim-dev-stac/hec-ras-stac/catalog.json ~/hec-ras-catalog-pilot/catalog.json
aws s3 sync s3://fimc-data/hv-fim-dev-stac/hec-ras-stac/mn/ ~/hec-ras-catalog-pilot/mn/
aws s3 sync s3://fimc-data/hv-fim-dev-stac/hec-ras-stac/nc/ ~/hec-ras-catalog-pilot/nc/

export PGPASSWORD=$(sudo cat /opt/hec-ras-stac/.db_password)
python3 load_catalog.py ~/hec-ras-catalog-pilot --db-host localhost --dry-run   # expect exactly 6 collections
sudo python3 load_catalog.py ~/hec-ras-catalog-pilot --db-host localhost
```

`max_locks_per_transaction` was already at the required value for this
instance, so the restart-required branch was never triggered at pilot scale
(see the full-rollout note below — this was re-checked before the full load).

Proxy URL rewrite, scoped explicitly by prefix (a `--batch` flag some tooling
provides only iterates hardcoded existing-program prefixes and would silently
skip new ones):
```bash
export HOST_IP=$(hostname -I | awk '{print $1}')
export PGPASSWORD=$(sudo cat /opt/hec-ras-stac/.db_password)

sudo -E python3 rewrite_asset_urls.py --proxy-url http://${HOST_IP}:8083 --db-host localhost --collection-prefix mn --dry-run
sudo -E python3 rewrite_asset_urls.py --proxy-url http://${HOST_IP}:8083 --db-host localhost --collection-prefix mn
sudo -E python3 rewrite_asset_urls.py --proxy-url http://${HOST_IP}:8083 --db-host localhost --collection-prefix nc --dry-run
sudo -E python3 rewrite_asset_urls.py --proxy-url http://${HOST_IP}:8083 --db-host localhost --collection-prefix nc
```

Verification:
```bash
docker exec -i hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "SELECT id FROM pgstac.collections WHERE id LIKE 'mn_%' OR id LIKE 'nc_%' ORDER BY id;"
# expect exactly the 6 pilot collection IDs

docker exec -i hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "SELECT COUNT(*) FROM pgstac.items WHERE (collection LIKE 'mn_%' OR collection LIKE 'nc_%') AND (content->'assets')::text LIKE '%s3://%';"
# expect 0 — no raw s3:// hrefs remain for mn/nc
```
Plus manually: thumbnails render and assets download (not 403) for a couple of
pilot items in the STAC Browser UI, and the asset-proxy smoke test
(`test_asset_proxy.sh`) was run before moving on to the downstream pipeline
test.

### End-to-end pilot test

All 6 pilot collections were run through the downstream model-processing
pipeline individually and inspected for clean completion (models
pulled/downloaded, pipeline steps completed, output artifacts produced).
Failures were triaged by iterating on `generate_nc_mn_catalog.py` and
redoing the affected collection where the issue traced back to STAC
structure; pipeline-internal issues unrelated to the STAC data were tracked
separately rather than blocking the rest of the pilot.

### Full rollout

```bash
mkdir -p ~/Desktop/work/nc-mn-migration-work/full/raw
cp -R ~/Desktop/work/dewberry-stac/mn/mn_* ~/Desktop/work/nc-mn-migration-work/full/raw/
cp -R ~/Desktop/work/dewberry-stac/nc/nc_* ~/Desktop/work/nc-mn-migration-work/full/raw/
python3 catalog-ops/generate_nc_mn_catalog.py \
  --raw-dir ~/Desktop/work/nc-mn-migration-work/full/raw \
  --out-dir ~/Desktop/work/nc-mn-migration-work/full \
  --data-root s3://fimc-data/hv-fim-dev-data \
  --reference-root-catalog ~/Desktop/work/hec-ras-stac/catalog.json \
  --dry-run
# review, then drop --dry-run
```

Metadata upload (dry-run first):
```bash
python3 migration-archive/upload_to_s3.py \
  --working-dir ~/Desktop/work/nc-mn-migration-work/full \
  --stac-root s3://fimc-data/hv-fim-dev-stac \
  --dry-run
# review, then drop --dry-run
```

Asset transfer, looped over all 292 collections (safely restartable — each
collection's sync is independent):
```bash
for cid in $(ls ~/Desktop/work/nc-mn-migration-work/full/raw); do
  aws s3 sync \
    "s3://fimc-data/dewberry-stac/hv-fim-dev-data/$cid/" \
    "s3://fimc-data/hv-fim-dev-data/hec-ras/$cid/"
done
```

Asset coverage verification, scoped to `mn`/`nc` naturally by pointing
`--working-dir` at the scratch dir (which only ever contains the new
collections):
```bash
python3 migration-archive/verify_asset_coverage.py \
  --data-root s3://fimc-data/hv-fim-dev-data \
  --working-dir ~/Desktop/work/nc-mn-migration-work/full \
  --full
```

**Full DB load:**
```bash
# Safety net first
/opt/hec-ras-stac/deployment/backup-db.sh

# On EC2, from /opt/hec-ras-stac/repo/catalog-ops/
mkdir -p ~/hec-ras-catalog-full
aws s3 cp s3://fimc-data/hv-fim-dev-stac/hec-ras-stac/catalog.json ~/hec-ras-catalog-full/catalog.json
aws s3 sync s3://fimc-data/hv-fim-dev-stac/hec-ras-stac/mn/ ~/hec-ras-catalog-full/mn/
aws s3 sync s3://fimc-data/hv-fim-dev-stac/hec-ras-stac/nc/ ~/hec-ras-catalog-full/nc/

export PGPASSWORD=$(sudo cat /opt/hec-ras-stac/.db_password)
python3 load_catalog.py ~/hec-ras-catalog-full --db-host localhost --dry-run
# expect exactly 292 collections
sudo python3 load_catalog.py ~/hec-ras-catalog-full --db-host localhost
```

`max_locks_per_transaction` re-checked before this step (8,434 items is much
closer to the scale where the original BLE/MIP migration actually hit a
`REFRESH MATERIALIZED VIEW partitions` lock-exhaustion error than the pilot's
6 items were):
```bash
docker exec hec-ras-stac-db psql -U pgstac -d stacdb -c "SHOW max_locks_per_transaction;"
```
If not already at the required value:
```bash
docker exec hec-ras-stac-db psql -U pgstac -d stacdb -c "ALTER SYSTEM SET max_locks_per_transaction = 256;"
docker restart hec-ras-stac-db   # briefly takes the live API offline
until docker exec hec-ras-stac-db pg_isready -U pgstac -d stacdb >/dev/null 2>&1; do sleep 2; done
```

Proxy URL rewrite, same pattern as the pilot:
```bash
export HOST_IP=$(hostname -I | awk '{print $1}')
export PGPASSWORD=$(sudo cat /opt/hec-ras-stac/.db_password)

sudo -E python3 rewrite_asset_urls.py --proxy-url http://${HOST_IP}:8083 --db-host localhost --collection-prefix mn --dry-run
sudo -E python3 rewrite_asset_urls.py --proxy-url http://${HOST_IP}:8083 --db-host localhost --collection-prefix mn
sudo -E python3 rewrite_asset_urls.py --proxy-url http://${HOST_IP}:8083 --db-host localhost --collection-prefix nc --dry-run
sudo -E python3 rewrite_asset_urls.py --proxy-url http://${HOST_IP}:8083 --db-host localhost --collection-prefix nc
```

Verification:
```bash
docker exec -i hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "SELECT COUNT(*) FROM pgstac.collections WHERE id LIKE 'mn_%' OR id LIKE 'nc_%';"
# expect 292

docker exec -i hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "SELECT COUNT(*) FROM pgstac.items WHERE collection LIKE 'mn_%' OR collection LIKE 'nc_%';"
# expect 8434

docker exec -i hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "SELECT COUNT(*) FROM pgstac.items WHERE (collection LIKE 'mn_%' OR collection LIKE 'nc_%') AND (content->'assets')::text LIKE '%s3://%';"
# expect 0 — no raw s3:// hrefs remain

docker exec -i hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "SELECT COUNT(*) FROM pgstac.collections;"   # expect 1431
docker exec -i hec-ras-stac-db psql -U pgstac -d stacdb -c \
  "SELECT COUNT(*) FROM pgstac.items;"          # expect 166607
```

Repo-wide counts updated: 1,139 + 292 = **1,431 collections**; 158,173 + 8,434
= **166,607 items** (`README.md`, `Catalog_Operations.md`,
`Deployment_Runbook.md`, this doc).

### Lessons for the next integration

- **Bucket-naming drift**: some of this repo's own docs (README, other
  operational docs) describe bare `s3://<bucket>` names that don't match what
  the live AWS account actually uses (a prefixed bucket name). Worth fixing
  those docs upstream so the next person doesn't hit the same confusion.
- **Catalog-loading tooling has no built-in collection/prefix filter** for
  most of the upload/sync scripts — plan for a scratch working directory
  approach from the start rather than looking for a flag that doesn't exist.
- **Always confirm the asset sync step actually ran** before trusting a
  collection's metadata is safe to load — a metadata-only load with missing
  assets fails silently until a downstream consumer tries to fetch a file.
