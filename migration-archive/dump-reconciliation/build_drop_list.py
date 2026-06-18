"""Reconcile the local S3 export against Dewberry's pgstac dump.

The dump is the final source of truth for what the migration publishes.
This script walks every local item JSON, looks it up in the dump, and
classifies it into one of eight buckets:

  A    Exact match (id, collection, source_hash all align with dump)        → MIGRATE
  B1   Local catalogued, dump has id under different collection,
       e_tags match → same physical model                                   → DROP
  B2a  Local mip_no_crs, dump has id under a real collection,
       different files → dump's corrected version supersedes ours           → DROP
  B2b  Local catalogued (not mip_no_crs), dump has id under different
       real collection, different files → name collision, distinct models   → KEEP
  C    Local catalogued, id absent from dump entirely                       → DROP
  D    Local uncatalogued, (id, source_hash) exact match in dump            → MIGRATE
       with collection override (use the dump's collection — local JSON's
       null collection field doesn't reflect Dewberry's catalog assignment)
  E    Local uncatalogued, id in dump under a different source_hash         → DROP
  F    Local uncatalogued, id absent from dump entirely                     → DROP

Items in MIGRATE/KEEP buckets (A + B2b + D) flow through. Bucket D
items get a `collection` override at rewrite_hrefs.py time, applied
via outputs/collection_overrides.tsv. Items in DROP buckets are
written to outputs/drop_list.txt.

Outputs:
  outputs/dump_items.tsv             — (id, collection, source_hash) for every dump item
  outputs/local_items.tsv            — (id, collection, source_hash, rel_path) for every local item
                                       rel_path is relative to --working-dir
  outputs/drop_classification.tsv    — every local item + its bucket + override (audit trail)
  outputs/drop_list.txt              — rel-paths to skip during migration
  outputs/collection_overrides.tsv   — rel_path → dump_collection for bucket-D items
"""
from __future__ import annotations

import argparse
import collections
import csv
import functools
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

# Unbuffered prints so progress shows up under `tee` and pipes.
print = functools.partial(print, flush=True)  # noqa: A001

try:
    import psycopg2
except ImportError:
    sys.stderr.write("psycopg2 not installed. Run: pip install psycopg2-binary\n")
    sys.exit(1)

HASH_RE = re.compile(r"/(ebfe|mip_30|mip_70)/(stac_items|source_models)/([a-zA-Z0-9_-]+)/")
SOURCE_PREFIXES = ("ebfe", "mip_30", "mip_70")
EXCLUDED_ASSET_KEYS = {"GeoPackage_file", "Thumbnail"}
ETAG_MATCH_THRESHOLD = 0.95


def _extract_hash(assets: dict) -> Optional[str]:
    """Return the first source-hash extractable from any asset HREF in this item, or None."""
    if not assets:
        return None
    for asset in assets.values():
        href = asset.get("href") if isinstance(asset, dict) else None
        if not href:
            continue
        m = HASH_RE.search(href)
        if m:
            return m.group(3)
    return None


def _dump_etags(assets: dict) -> dict[str, str]:
    """Return {asset_key: e_tag} for the dump record, excluding derived artefacts."""
    out: dict[str, str] = {}
    if not assets:
        return out
    for k, v in assets.items():
        if k in EXCLUDED_ASSET_KEYS:
            continue
        if not isinstance(v, dict):
            continue
        et = v.get("e_tag")
        if et:
            out[k] = et
    return out


def _local_etags(item: dict) -> dict[str, str]:
    """Return {asset_key: e_tag} for the local item, excluding derived artefacts."""
    out: dict[str, str] = {}
    for k, v in (item.get("assets") or {}).items():
        if k in EXCLUDED_ASSET_KEYS:
            continue
        if not isinstance(v, dict):
            continue
        et = v.get("e_tag")
        if et:
            out[k] = et
    return out


def _etag_match_rate(local: dict[str, str], dump: dict[str, str]) -> tuple[float, int]:
    """Return (fraction_matching, overlap_size). Overlap = keys present on both sides."""
    overlap = set(local) & set(dump)
    if not overlap:
        return 0.0, 0
    matches = sum(1 for k in overlap if local[k] == dump[k])
    return matches / len(overlap), len(overlap)


def _load_dump_index(host: str, port: int, user: str, password: str, dbname: str
                     ) -> tuple[dict[tuple[str, str], str],
                                dict[str, dict[str, dict[str, str]]],
                                dict[str, set[str]],
                                dict[tuple[str, str], str],
                                dict[str, list[tuple[str, str]]]]:
    """Fetch the dump's items table and build four indexes:

    - dump_idx[(id, collection)]            = source_hash_dir
    - id_to_dump_etags[id][collection]      = {asset_key: e_tag}     (for bucket-B disambiguation)
    - id_to_dump_hashes[id]                 = set of source_hashes   (for bucket-D/E/F classification)
    - hash_coll_to_dump_id[(hash, coll)]    = dump_id                (for catalogued suffix-mismatch detection)

    The dump assigns _N numeric suffixes to items whose IDs collide within a
    collection. The S3 export stores the same items without the suffix. The
    fourth index lets the classifier detect these suffix-only mismatches and
    treat them as bucket-A matches (for catalogued items) or bucket-D matches
    (for uncatalogued items).
    """
    conn = psycopg2.connect(host=host, port=port, user=user, password=password, dbname=dbname)
    conn.set_session(readonly=True)  # autocommit off so server-side cursor works
    dump_idx: dict[tuple[str, str], str] = {}
    id_to_etags: dict[str, dict[str, dict[str, str]]] = collections.defaultdict(dict)
    id_to_hashes: dict[str, set[str]] = collections.defaultdict(set)
    hash_coll_to_dump_id: dict[tuple[str, str], str] = {}
    hash_to_dump_entries: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    with conn.cursor("dump_scan") as cur:
        cur.itersize = 5000
        cur.execute("SELECT id, collection, content->'assets' FROM pgstac.items")
        for iid, coll, assets in cur:
            h = _extract_hash(assets or {})
            if h is None:
                continue
            dump_idx[(iid, coll)] = h
            id_to_etags[iid][coll] = _dump_etags(assets or {})
            id_to_hashes[iid].add(h)
            hash_coll_to_dump_id[(h, coll)] = iid
            hash_to_dump_entries[h].append((iid, coll))
    conn.commit()
    conn.close()
    return dump_idx, id_to_etags, id_to_hashes, hash_coll_to_dump_id, hash_to_dump_entries


def _iter_local_items(items_root: Path, working_dir: Path):
    """Yield (id, collection, source_hash_dir, rel_path, etags) per local item.

    rel_path is relative to working_dir so outputs stay machine-portable
    (e.g. "items/ebfe/<hash>/<id>.json"). Consumers reconstruct the absolute
    path by joining with their own working_dir.

    Loads only the fields we need from each JSON, dropping the rest to keep memory bounded.
    """
    for prefix in SOURCE_PREFIXES:
        pdir = items_root / prefix
        if not pdir.exists():
            sys.stderr.write(f"  warning: {pdir} not found, skipping\n")
            continue
        for path in pdir.rglob("*.json"):
            try:
                with path.open() as f:
                    item = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                sys.stderr.write(f"  skip {path}: {e}\n")
                continue
            if item.get("type") != "Feature":
                continue
            iid = item.get("id")
            if not iid:
                continue
            coll = item.get("collection")
            hash_dir = path.parent.name
            rel_path = str(path.relative_to(working_dir))
            yield iid, coll, hash_dir, rel_path, _local_etags(item)


def _dump_collection_for_hash(
    iid: str,
    local_hash: str,
    dump_idx: dict[tuple[str, str], str],
) -> Optional[str]:
    """For a given id, return the dump collection whose source_hash matches local_hash.

    There can be multiple dump entries for an id (same id under different
    collections). We want the one with the matching hash — that's the
    item we'd be migrating if the local copy had a collection set.
    Returns None if no match (shouldn't happen for a true bucket-D candidate).
    """
    for (d_id, d_coll), d_hash in dump_idx.items():
        if d_id == iid and d_hash == local_hash:
            return d_coll
    return None


def _classify(
    iid: str,
    local_coll: Optional[str],
    local_hash: str,
    local_etags: dict[str, str],
    dump_idx: dict[tuple[str, str], str],
    id_to_dump_etags: dict[str, dict[str, dict[str, str]]],
    id_to_dump_hashes: dict[str, set[str]],
    hash_coll_to_dump_id: dict[tuple[str, str], str],
    hash_to_dump_entries: dict[str, list[tuple[str, str]]],
) -> tuple[str, str, Optional[str], Optional[str]]:
    """Apply the eight-bucket rule.

    Returns (bucket, decision, override_collection, id_rewrite).
    - override_collection is non-None only for bucket D (null→real collection).
    - id_rewrite is non-None when the dump published this item under a suffixed
      id (e.g. dump id=ADAMS_BAYOU_1, local id=ADAMS_BAYOU). rewrite_hrefs.py
      applies this to keep the destination id in sync with the dump's catalog.
    """
    if local_coll is not None:
        # Catalogued local — exact match
        key = (iid, local_coll)
        if key in dump_idx and dump_idx[key] == local_hash:
            return "A", "MIGRATE", None, None
        # The dump may have applied a _N numeric suffix to this id to resolve
        # within-collection duplicates. Check by (hash, collection): if the dump
        # has our (hash, collection) under a suffixed id, this is the same
        # physical item — treat as bucket A and record the id rewrite.
        dump_id = hash_coll_to_dump_id.get((local_hash, local_coll))
        if dump_id is not None:
            return "A", "MIGRATE", None, dump_id
        if iid not in id_to_dump_hashes:
            return "C", "DROP", None, None
        # id appears in dump but not under our collection — bucket B
        best_rate = 0.0
        for d_etags in id_to_dump_etags.get(iid, {}).values():
            rate, _overlap = _etag_match_rate(local_etags, d_etags)
            if rate > best_rate:
                best_rate = rate
        if best_rate >= ETAG_MATCH_THRESHOLD:
            return "B1", "DROP", None, None
        if local_coll == "mip_no_crs":
            return "B2a", "DROP", None, None
        return "B2b", "KEEP", None, None
    else:
        # Uncatalogued local
        dump_hashes = id_to_dump_hashes.get(iid)
        if dump_hashes and local_hash in dump_hashes:
            # Dump has this exact (id, hash). Local is uncatalogued but the
            # dump assigns it to a real collection — migrate with override.
            override = _dump_collection_for_hash(iid, local_hash, dump_idx)
            return "D", "MIGRATE", override, None
        # The dump may store this item under a suffixed id (id_N). Check the
        # hash-only reverse index to find any dump entry with the same files.
        dump_entries = hash_to_dump_entries.get(local_hash)
        if dump_entries:
            dump_id, dump_coll = dump_entries[0]
            return "D", "MIGRATE", dump_coll, dump_id
        if not dump_hashes:
            return "F", "DROP", None, None
        return "E", "DROP", None, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--working-dir", type=Path, default=Path(os.environ.get("WORKING_DIR", Path.home() / "ras-stac-migration")),
                        help="local working dir; expects <working-dir>/items/<prefix>/<hash>/*.json")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "outputs")
    parser.add_argument("--pgstac-host", default="localhost")
    parser.add_argument("--pgstac-port", type=int, default=5433)
    parser.add_argument("--pgstac-user", default="pgstac")
    parser.add_argument("--pgstac-password", default="pgstac")
    parser.add_argument("--pgstac-name", default="stacdb")
    args = parser.parse_args()
    args.working_dir = args.working_dir.expanduser().resolve()
    args.output_dir = args.output_dir.expanduser().resolve()

    items_root = args.working_dir / "items"
    if not items_root.exists():
        sys.stderr.write(f"error: {items_root} does not exist\n")
        return 1
    args.output_dir.mkdir(parents=True, exist_ok=True)

    print(f"==> Loading dump index from {args.pgstac_host}:{args.pgstac_port}/{args.pgstac_name}")
    t0 = time.time()
    dump_idx, id_to_dump_etags, id_to_dump_hashes, hash_coll_to_dump_id, hash_to_dump_entries = _load_dump_index(
        args.pgstac_host, args.pgstac_port, args.pgstac_user, args.pgstac_password, args.pgstac_name,
    )
    print(f"    dump items: {len(dump_idx)}, unique ids: {len(id_to_dump_hashes)}  ({time.time()-t0:.0f}s)")

    # Persist dump-side index for audit
    dump_tsv = args.output_dir / "dump_items.tsv"
    with dump_tsv.open("w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["id", "collection", "source_hash"])
        for (iid, coll), h in sorted(dump_idx.items()):
            w.writerow([iid, coll, h])
    print(f"    wrote {dump_tsv}")

    print(f"==> Walking local items at {items_root}")
    t0 = time.time()
    local_tsv = args.output_dir / "local_items.tsv"
    classification_tsv = args.output_dir / "drop_classification.tsv"
    drop_txt = args.output_dir / "drop_list.txt"
    overrides_tsv = args.output_dir / "collection_overrides.tsv"
    id_rewrites_tsv = args.output_dir / "id_rewrites.tsv"

    bucket_counts: collections.Counter = collections.Counter()
    decisions: collections.Counter = collections.Counter()
    drop_paths: list[str] = []
    overrides: list[tuple[str, str]] = []  # (rel_path, override_collection)
    id_rewrites: list[tuple[str, str, str]] = []  # (rel_path, local_id, dump_id)

    with local_tsv.open("w", newline="") as f_local, \
         classification_tsv.open("w", newline="") as f_class:
        w_local = csv.writer(f_local, delimiter="\t")
        w_local.writerow(["id", "collection", "source_hash", "rel_path"])
        w_class = csv.writer(f_class, delimiter="\t")
        w_class.writerow(["bucket", "decision", "id", "collection", "source_hash", "rel_path",
                          "override_collection", "id_rewrite"])

        seen = 0
        for iid, coll, hash_dir, path, etags in _iter_local_items(items_root, args.working_dir):
            w_local.writerow([iid, coll if coll is not None else "", hash_dir, path])
            bucket, decision, override, id_rewrite = _classify(
                iid, coll, hash_dir, etags, dump_idx, id_to_dump_etags, id_to_dump_hashes,
                hash_coll_to_dump_id, hash_to_dump_entries,
            )
            w_class.writerow([bucket, decision, iid, coll if coll is not None else "", hash_dir, path,
                              override or "", id_rewrite or ""])
            bucket_counts[bucket] += 1
            decisions[decision] += 1
            if decision == "DROP":
                drop_paths.append(path)
            if override:
                overrides.append((path, override))
            if id_rewrite:
                id_rewrites.append((path, iid, id_rewrite))
            seen += 1
            if seen % 20000 == 0:
                print(f"    ...{seen} items classified")

    with drop_txt.open("w") as f:
        f.write(f"# generated by build_drop_list.py\n")
        f.write(f"# paths are relative to the migration working dir "
                f"(e.g. items/ebfe/<hash>/<id>.json)\n")
        f.write(f"# total drops: {len(drop_paths)}\n")
        for p in sorted(drop_paths):
            f.write(p + "\n")

    with overrides_tsv.open("w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["rel_path", "override_collection"])
        for p, oc in sorted(overrides):
            w.writerow([p, oc])

    with id_rewrites_tsv.open("w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["rel_path", "local_id", "dump_id"])
        for p, lid, did in sorted(id_rewrites):
            w.writerow([p, lid, did])

    elapsed = time.time() - t0
    total = sum(bucket_counts.values())
    print(f"    wrote {local_tsv}")
    print(f"    wrote {classification_tsv}")
    print(f"    wrote {drop_txt}")
    print(f"    wrote {overrides_tsv}  ({len(overrides)} bucket-D overrides)")
    print(f"    wrote {id_rewrites_tsv}  ({len(id_rewrites)} id rewrites for dump suffix-mismatch items)")
    print(f"    classified {total} local items in {elapsed:.0f}s")

    print()
    print(f"==> Bucket distribution")
    bucket_order = ["A", "B1", "B2a", "B2b", "C", "D", "E", "F"]
    for b in bucket_order:
        n = bucket_counts.get(b, 0)
        print(f"      {b:<4} {n:>7}")
    print(f"      {'-'*4} {'-'*7}")
    print(f"      {'sum':<4} {total:>7}")
    print()
    print(f"==> Decision summary")
    print(f"      MIGRATE: {decisions.get('MIGRATE', 0)} + {decisions.get('KEEP', 0)} = {decisions.get('MIGRATE', 0) + decisions.get('KEEP', 0)}")
    print(f"      DROP:    {decisions.get('DROP', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
