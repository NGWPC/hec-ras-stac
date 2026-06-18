"""
Fetch collection metadata from Dewberry's STAC API and write it locally.

Source: https://stac2.dewberryanalytics.com/collections
Output: <working_dir>/collections/<collection-id>/collection.json

Default: walks <working_dir>/items/ first to discover which collection IDs
are actually represented AFTER the drop-list pre-flight, then writes ONLY
those. This matches the dump-as-source-of-truth posture — every collection
that ends up in the destination has at least one item migrating into it.

The drop list is read from drop_list.txt and
contains rel-paths (e.g. "items/ebfe/<hash>/<id>.json"). Items whose
rel-path appears in the drop list are skipped during the discovery walk
so their collection isn't pulled into the destination just because their
JSON references it.

With `--all`: writes every collection Dewberry's API surfaces, including
empty-but-named placeholders. Reserved for special cases (e.g. seeding an
a full `--all` catalog seed).

Link `href`s in fetched collections are left untouched — stac-fastapi
rewrites links at serve time, so the durable S3 copy can carry source links
for provenance.

Usage:
    python generate_collections.py                                  # default: drop-list-aware filter
    python generate_collections.py --all                            # every collection from the API
    python generate_collections.py --working-dir ~/ras-stac-migration-subset
    python generate_collections.py --drop-list /path/to/drop_list.txt
    python generate_collections.py --dry-run
"""

import argparse
import json
import logging
import os
import sys
import urllib.request
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

SOURCE_STAC_URL = "https://stac2.dewberryanalytics.com"
DEFAULT_DROP_LIST = Path(__file__).parent / "drop_list.txt"
DEFAULT_OVERRIDES = Path(__file__).parent / "collection_overrides.tsv"


def fetch_all_collections(source_url: str) -> list[dict]:
    url = f"{source_url}/collections?limit=2000"
    logger.info(f"Fetching collections from {url} ...")
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read())
    collections = data["collections"]
    logger.info(f"Fetched {len(collections)} collections")
    return collections


def load_drop_set(drop_list_path: Optional[Path], working_dir: Path) -> set[str]:
    """Return drop-list entries resolved as absolute paths under working_dir.

    drop_list.txt stores rel-paths; we resolve them against working_dir so the
    set can be compared directly against paths produced by items_dir.rglob.
    """
    if drop_list_path is None or not drop_list_path.exists():
        return set()
    out: set[str] = set()
    sep = os.sep
    with drop_list_path.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.add(str(working_dir) + sep + line.replace("/", sep))
    return out


def load_overrides(overrides_path: Optional[Path], working_dir: Path) -> dict[str, str]:
    """Return {absolute_path: override_collection} from collection_overrides.tsv."""
    if overrides_path is None or not overrides_path.exists():
        return {}
    out: dict[str, str] = {}
    import csv
    with overrides_path.open() as f:
        for row in csv.DictReader(f, delimiter="\t"):
            rel = row.get("rel_path", "").strip()
            col = row.get("override_collection", "").strip()
            if rel and col:
                out[str(working_dir / rel)] = col
    return out


def discover_referenced_collection_ids(
    items_dir: Path, drop_paths: set[str], overrides: dict[str, str]
) -> set[str]:
    """Walk items_dir and return the set of collection IDs that surviving items reference.

    Items in drop_paths are skipped. Items with collection: null are checked
    against overrides (bucket D) so their override collection is included.
    """
    referenced: set[str] = set()
    if not items_dir.exists():
        return referenced
    for p in items_dir.rglob("*.json"):
        abs_path = str(p)
        if abs_path in drop_paths:
            continue
        try:
            d = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        cid = d.get("collection") or overrides.get(abs_path)
        if cid:
            referenced.add(cid)
    return referenced


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch + write collection.json files for collections referenced by surviving local items")
    parser.add_argument("--working-dir", default=os.environ.get("WORKING_DIR", "~/ras-stac-migration"))
    parser.add_argument("--source-url", default=SOURCE_STAC_URL)
    parser.add_argument(
        "--drop-list",
        type=Path,
        default=DEFAULT_DROP_LIST,
        help=f"Drop list of local item rel-paths to skip (default: {DEFAULT_DROP_LIST}). "
             "Produced by dump-reconciliation/build_drop_list.py.",
    )
    parser.add_argument(
        "--overrides",
        type=Path,
        default=DEFAULT_OVERRIDES,
        help=f"Collection overrides TSV for bucket-D items (default: {DEFAULT_OVERRIDES}). "
             "Produced by dump-reconciliation/build_drop_list.py.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Emit every collection from Dewberry's API. Default: filter to only those referenced by surviving local items in <working-dir>/items/.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    working_dir = Path(args.working_dir).expanduser().resolve()
    items_dir = working_dir / "items"
    collections_dir = working_dir / "collections"

    all_collections = fetch_all_collections(args.source_url)
    api_index = {c["id"]: c for c in all_collections}

    if args.all:
        filtered = list(api_index.values())
    else:
        drop_paths = load_drop_set(args.drop_list, working_dir)
        if drop_paths:
            logger.info(f"Loaded drop list: {len(drop_paths)} paths to exclude from discovery")
        else:
            logger.warning(f"Drop list not found or empty at {args.drop_list}; "
                           "every locally-referenced collection will be emitted")
        overrides = load_overrides(args.overrides, working_dir)
        if overrides:
            logger.info(f"Loaded collection overrides: {len(overrides)} bucket-D entries")
        referenced = discover_referenced_collection_ids(items_dir, drop_paths, overrides)
        if not referenced:
            logger.error(f"no surviving item JSONs found under {items_dir} — run sync_items.py first, "
                         "or use --all to write every collection unconditionally")
            return 1
        logger.info(f"{len(referenced)} distinct collection ids referenced by surviving items")
        missing_from_api = referenced - set(api_index)
        if missing_from_api:
            logger.warning(f"{len(missing_from_api)} referenced ids not on Dewberry's API "
                           f"(skipped): {sorted(missing_from_api)}")
        filtered = [api_index[cid] for cid in sorted(referenced) if cid in api_index]

    logger.info(f"Writing {len(filtered)} collection.json files")

    if args.dry_run:
        for c in filtered:
            logger.info(f"[DRY RUN] would write {collections_dir}/{c['id']}/collection.json")
        return 0

    collections_dir.mkdir(parents=True, exist_ok=True)
    for collection in filtered:
        out = collections_dir / collection["id"] / "collection.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(collection, indent=2))

    logger.info(f"Wrote {len(filtered)} collection.json files to {collections_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
