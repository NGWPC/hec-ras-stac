"""
Rewrite asset HREFs in local item JSONs AND restructure the local items dir
to match the destination layout.

Reads from:  <working_dir>/items/<source_prefix>/<source_hash_dir>/<item-id>.json
                                                                        (source shape, from sync_items.py)
Writes to:   <working_dir>/items/<collection-id>/<item-id>/<item-id>.json
                                                                        (destination shape)

Per item:

1. Drop-list check. If the item's local path appears in
   `drop_list.txt` (produced by build_drop_list.py),
   the item is skipped: the source-shape JSON is deleted from the working
   dir and the per-item walk moves on. This is how the migration mirrors
   Dewberry's pgstac dump — items not in the dump get filtered out here.

2. Rewrite asset HREFs to the destination data root:
       <data-root>/hec-ras/<collection-id>/<item-id>/<file>
   Also sets `assets[*].s3_key` to the bucket-relative key.

3. Set legacy-source provenance properties (`original_source_hash`,
   `original_source_hash_dir`, `original_source_prefix`) on every item.
   Lets sync_assets.py reconstruct the source key without needing the
   original HREFs. Safe to drop post-migration once the Dewberry source
   bucket is decommissioned.

4. Move the JSON to its destination-layout location. After all items are
   processed, prune the now-empty <source_prefix>/<source_hash_dir>/ and
   <source_prefix>/ dirs.

Idempotent: items already in destination shape (path's first segment is
not a known source prefix) are skipped without changes.

`--data-root` is an S3 URI: `s3://<bucket>` (production) or
`s3://<bucket>/<key-prefix>` (multi-tenant / test). The script appends
`/hec-ras/...` after it.

Usage:
    python rewrite_hrefs.py --data-root s3://hv-fim-dev-data
    python rewrite_hrefs.py --data-root s3://fimc-data/test-hv-fim-dev-data
    python rewrite_hrefs.py --drop-list /path/to/drop_list.txt
    python rewrite_hrefs.py ... --dry-run
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# The three Dewberry source-prefix directory names. Authoritative: items
# under these paths use the source-side hash-dir shape (and need
# restructuring); items under any other top-level dir are in the
# destination layout and pass through untouched.
SOURCE_PREFIXES: frozenset[str] = frozenset({"ebfe", "mip_30", "mip_70"})

DEFAULT_DROP_LIST = Path(__file__).parent / "drop_list.txt"
DEFAULT_OVERRIDES = Path(__file__).parent / "collection_overrides.tsv"
DEFAULT_ID_REWRITES = Path(__file__).parent / "id_rewrites.tsv"


def _derive_source_hash(hash_dir_name: str) -> str:
    """Extract the content-addressed hash from a source hash-dir name.

    Source layout uses two naming conventions:
      ebfe:           '<hex-hash>'                  → return whole name
      mip_30/mip_70:  '<fema-case>_<hex-hash>'      → return part after last '_'
    """
    return hash_dir_name.rsplit("_", 1)[-1]


def validate_s3_root(uri: str, flag: str) -> str:
    """Validate an S3 root URI: s3://bucket[/prefix], no trailing slash. Returns the normalized URI."""
    if not uri.startswith("s3://"):
        raise argparse.ArgumentTypeError(f"{flag} must start with 's3://' (got '{uri}')")
    if uri.endswith("/"):
        raise argparse.ArgumentTypeError(f"{flag} must not have a trailing slash (got '{uri}')")
    tail = uri[len("s3://"):]
    if not tail or tail.startswith("/"):
        raise argparse.ArgumentTypeError(f"{flag} must include a bucket after 's3://' (got '{uri}')")
    return uri


def parse_s3_root(uri: str) -> tuple[str, str]:
    """Split s3://bucket[/prefix] into (bucket, prefix). prefix may be empty."""
    tail = uri[len("s3://"):]
    bucket, _, prefix = tail.partition("/")
    return bucket, prefix


def _parse_source_href(href: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Parse a source HREF into (prefix, hash, rel_path). Section (source_models/stac_items) is dropped."""
    if "fim.s3.amazonaws.com" in href:
        href = href.replace("https://fim.s3.amazonaws.com//", "s3://fim/")
        href = href.replace("https://fim.s3.amazonaws.com/", "s3://fim/")
    if not href.startswith("s3://fim/"):
        return None, None, None
    key = urlparse(href).path.lstrip("/")
    parts = key.split("/", 3)
    if len(parts) < 4:
        return None, None, None
    prefix, _section, hash_dir, rel_path = parts
    return prefix, hash_dir, rel_path


def _destination_href(data_root: str, collection_id: str, item_id: str, rel_path: str) -> str:
    """Build a destination-shape HREF."""
    return f"{data_root}/hec-ras/{collection_id}/{item_id}/{rel_path}"


def _is_source_path(path: Path, items_dir: Path) -> bool:
    """True iff path is under a known source-prefix dir (items/<source-prefix>/...)."""
    try:
        rel = path.relative_to(items_dir)
    except ValueError:
        return False
    return len(rel.parts) >= 1 and rel.parts[0] in SOURCE_PREFIXES


def _load_drop_list(drop_list_path: Optional[Path], working_dir: Path) -> set[str]:
    """Load the drop list as a set of absolute paths.

    drop_list.txt stores paths relative to the migration working dir
    (e.g. "items/ebfe/<hash>/<id>.json"). This function joins each
    relative entry with `working_dir` so per-item walk comparisons match
    the absolute paths that `path.rglob` produces.
    """
    if drop_list_path is None or not drop_list_path.exists():
        return set()
    paths: set[str] = set()
    sep = os.sep
    with drop_list_path.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Use string join rather than Path / line so that backslashes in
            # filenames (literal POSIX filenames from Dewberry) are preserved
            # as-is on all platforms instead of being interpreted as separators.
            paths.add(str(working_dir) + sep + line.replace("/", sep))
    return paths


def _load_id_rewrites(rewrites_path: Optional[Path], working_dir: Path) -> dict[str, str]:
    """Load id rewrite map keyed by absolute local path.

    id_rewrites.tsv has columns: rel_path, local_id, dump_id.
    Returns {abs_path: dump_id} for items where the dump used a _N suffixed id.
    rewrite_hrefs.py applies this to set item['id'] to the dump's canonical value.
    """
    if rewrites_path is None or not rewrites_path.exists():
        return {}
    import csv
    out: dict[str, str] = {}
    with rewrites_path.open() as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            rel = row.get("rel_path", "").strip()
            dump_id = row.get("dump_id", "").strip()
            if rel and dump_id:
                out[str(working_dir) + os.sep + rel.replace("/", os.sep)] = dump_id
    return out


def _load_collection_overrides(overrides_path: Optional[Path], working_dir: Path) -> dict[str, str]:
    """Load bucket-D collection overrides keyed by absolute local path.

    collection_overrides.tsv has columns: rel_path, override_collection.
    Same rel-path convention as drop_list.txt — resolve against working_dir.
    """
    if overrides_path is None or not overrides_path.exists():
        return {}
    out: dict[str, str] = {}
    import csv
    with overrides_path.open() as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            rel = row.get("rel_path", "").strip()
            coll = row.get("override_collection", "").strip()
            if rel and coll:
                out[str(working_dir) + os.sep + rel.replace("/", os.sep)] = coll
    return out


def rewrite_item(path: Path, items_dir: Path, data_root: str, data_bucket: str,
                 dry_run: bool, drop_paths: set[str], collection_overrides: dict[str, str],
                 id_rewrites: dict[str, str]) -> dict:
    """Rewrite one item JSON and move it to its destination location.

    Returns a dict of counters: {assets_changed, moved, skipped_drop_list,
    skipped_already_destination, skipped_missing_collection, collection_overridden,
    id_rewritten}.
    """
    counters = {"assets_changed": 0, "moved": 0,
                "skipped_drop_list": 0, "skipped_already_destination": 0,
                "skipped_missing_collection": 0, "collection_overridden": 0,
                "id_rewritten": 0}

    # Drop-list filter: items dropped by build_drop_list.py never make it
    # to the destination. Source-shape JSON is removed from the working dir.
    if str(path) in drop_paths:
        counters["skipped_drop_list"] = 1
        if not dry_run:
            path.unlink()
        return counters

    # Idempotence: skip anything not in source-shape under items_dir.
    if not _is_source_path(path, items_dir):
        counters["skipped_already_destination"] = 1
        return counters

    try:
        item = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        logger.warning(f"Skipping {path}: {e}")
        return counters

    # Apply bucket-D collection override: local JSONs in this bucket have
    # `collection: null` but the dump assigns them to a real collection.
    # build_drop_list.py emits the mapping in collection_overrides.tsv.
    override = collection_overrides.get(str(path))
    if override:
        item["collection"] = override
        counters["collection_overridden"] = 1

    collection_id = item.get("collection")
    if not collection_id:
        # Surviving items must have a real collection (dump-reconciliation
        # guarantees no null-collection items reach this point). If we hit
        # one, it's a sign the drop list / overrides are stale or wasn't applied —
        # log and skip rather than route it to a synthetic bucket.
        logger.warning(f"Skipping {path}: collection is null/missing and item not on drop list or overrides")
        counters["skipped_missing_collection"] = 1
        return counters

    # Apply id rewrite: the dump published this item under a _N suffixed id.
    # rewrite item['id'] to match so the destination catalog mirrors the dump exactly.
    id_rewrite = id_rewrites.get(str(path))
    if id_rewrite:
        item["id"] = id_rewrite
        counters["id_rewritten"] = 1

    item_id = item["id"]

    # Legacy-source provenance. Lets future operators reconstruct the
    # Dewberry source S3 key for any item, and lets sync_assets.py fetch
    # source files without needing the original HREFs.
    #
    #   source key = s3://fimc-data/dewberry-stac/<original_source_prefix>/
    #                  {source_models|stac_items}/<original_source_hash_dir>/...
    rel = path.relative_to(items_dir)
    hash_dir_name = rel.parts[1]  # items/<source_prefix>/<hash_dir>/<id>.json
    source_prefix = rel.parts[0]
    props = item.setdefault("properties", {})
    props["original_source_hash"] = _derive_source_hash(hash_dir_name)
    props["original_source_hash_dir"] = hash_dir_name
    props["original_source_prefix"] = source_prefix

    # Rewrite asset HREFs to the destination shape
    bucket_uri = f"s3://{data_bucket}/"
    for asset in item.get("assets", {}).values():
        old_href = asset.get("href", "")
        prefix, hash_dir, rel_path = _parse_source_href(old_href)
        if prefix is None:
            continue
        new_href = _destination_href(data_root, collection_id, item_id, rel_path)
        if new_href != old_href:
            asset["href"] = new_href
            asset["s3_key"] = new_href.replace(bucket_uri, "", 1)
            counters["assets_changed"] += 1

    # Compute the new local path: items/<collection>/<item_id>/<item_id>.json
    new_path = items_dir / collection_id / item_id / f"{item_id}.json"

    if dry_run:
        logger.info(f"[DRY RUN] {rel} → {new_path.relative_to(items_dir)}")
        return counters

    new_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.write_text(json.dumps(item, indent=2))
    if new_path != path:
        path.unlink()
        counters["moved"] = 1
    return counters


def _prune_empty_dirs(items_dir: Path, source_prefixes: list[str], dry_run: bool) -> int:
    """After all moves, remove the now-empty <source_prefix>/<hash_dir>/ and <source_prefix>/ dirs."""
    pruned = 0
    for prefix in source_prefixes:
        prefix_dir = items_dir / prefix
        if not prefix_dir.exists():
            continue
        for hash_dir in sorted(prefix_dir.iterdir()):
            if hash_dir.is_dir() and not any(hash_dir.iterdir()):
                if dry_run:
                    logger.info(f"[DRY RUN] rmdir {hash_dir.relative_to(items_dir)}")
                else:
                    hash_dir.rmdir()
                pruned += 1
        if prefix_dir.exists() and not any(prefix_dir.iterdir()):
            if dry_run:
                logger.info(f"[DRY RUN] rmdir {prefix_dir.relative_to(items_dir)}")
            else:
                prefix_dir.rmdir()
            pruned += 1
    return pruned


def _present_source_prefixes(items_dir: Path) -> list[str]:
    """Return source-prefix subdir names actually present under items/."""
    return sorted(p.name for p in items_dir.iterdir()
                  if p.is_dir() and p.name in SOURCE_PREFIXES)


def main() -> int:
    parser = argparse.ArgumentParser(description="Rewrite asset HREFs + restructure items source → destination layout")
    parser.add_argument("--working-dir", default=os.environ.get("WORKING_DIR", "~/ras-stac-migration"))
    parser.add_argument(
        "--data-root",
        required=True,
        type=lambda v: validate_s3_root(v, "--data-root"),
        help="Destination data root S3 URI, e.g. s3://hv-fim-dev-data or s3://fimc-data/test-hv-fim-dev-data",
    )
    parser.add_argument(
        "--drop-list",
        type=Path,
        default=DEFAULT_DROP_LIST,
        help=f"Drop list of local item paths to skip (default: {DEFAULT_DROP_LIST}). "
             "Produced by dump-reconciliation/build_drop_list.py.",
    )
    parser.add_argument(
        "--collection-overrides",
        type=Path,
        default=DEFAULT_OVERRIDES,
        help=f"TSV mapping rel_path → override_collection for bucket-D items "
             f"(default: {DEFAULT_OVERRIDES}). Produced by build_drop_list.py.",
    )
    parser.add_argument(
        "--id-rewrites",
        type=Path,
        default=DEFAULT_ID_REWRITES,
        help=f"TSV mapping rel_path → dump_id for items where the dump used a _N suffixed id "
             f"(default: {DEFAULT_ID_REWRITES}). Produced by build_drop_list.py.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    working_dir = Path(args.working_dir).expanduser().resolve()
    items_dir = working_dir / "items"
    if not items_dir.exists():
        logger.error(f"Items dir not found: {items_dir} — run sync_items.py first")
        return 1

    drop_paths = _load_drop_list(args.drop_list, working_dir)
    if drop_paths:
        logger.info(f"Loaded drop list: {len(drop_paths)} paths to skip from {args.drop_list}")
    else:
        logger.warning(f"Drop list not found or empty at {args.drop_list}; nothing will be skipped. "
                       "Run dump-reconciliation/build_drop_list.py first.")

    collection_overrides = _load_collection_overrides(args.collection_overrides, working_dir)
    if collection_overrides:
        logger.info(f"Loaded collection overrides: {len(collection_overrides)} bucket-D items "
                    f"from {args.collection_overrides}")
    else:
        logger.info(f"No collection overrides found at {args.collection_overrides}; "
                    "items with null collection will be skipped.")

    id_rewrites = _load_id_rewrites(args.id_rewrites, working_dir)
    if id_rewrites:
        logger.info(f"Loaded id rewrites: {len(id_rewrites)} suffix-mismatch items "
                    f"from {args.id_rewrites}")
    else:
        logger.info(f"No id rewrites found at {args.id_rewrites}; item ids will not be rewritten.")

    source_prefixes = _present_source_prefixes(items_dir)
    if not source_prefixes:
        logger.info("No source-prefix dirs found under items/ — nothing to do (already restructured?)")
        return 0
    logger.info(f"Found source-prefix dirs: {source_prefixes}")

    json_files = [p for prefix in source_prefixes
                  for p in (items_dir / prefix).rglob("*.json")]
    logger.info(f"Found {len(json_files)} source-shape item JSONs in {items_dir}")
    logger.info(f"Data root: {args.data_root}")
    if args.dry_run:
        logger.info("DRY RUN — no files will be modified")

    data_bucket, _ = parse_s3_root(args.data_root)

    totals = {"assets_changed": 0, "moved": 0,
              "skipped_drop_list": 0, "skipped_already_destination": 0,
              "skipped_missing_collection": 0, "collection_overridden": 0, "id_rewritten": 0}
    for path in json_files:
        c = rewrite_item(path, items_dir, args.data_root, data_bucket, args.dry_run,
                         drop_paths, collection_overrides, id_rewrites)
        for k in totals:
            totals[k] += c[k]

    pruned = _prune_empty_dirs(items_dir, source_prefixes, args.dry_run)

    logger.info(f"Items processed:                       {len(json_files)}")
    logger.info(f"Items moved to destination layout:     {totals['moved']}")
    logger.info(f"  of which had collection override:    {totals['collection_overridden']}")
    logger.info(f"  of which had id rewrite:             {totals['id_rewritten']}")
    logger.info(f"Items skipped (drop list):             {totals['skipped_drop_list']}")
    logger.info(f"Items skipped (already destination):   {totals['skipped_already_destination']}")
    logger.info(f"Items skipped (missing collection):    {totals['skipped_missing_collection']}")
    logger.info(f"Asset HREFs rewritten:                 {totals['assets_changed']}")
    logger.info(f"Empty source-prefix dirs pruned:       {pruned}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
