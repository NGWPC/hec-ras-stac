"""
Regenerate catalog.json / collection.json / item JSONs for a state-portal STAC
delivery (built for NC/MN, see catalog-ops/Model_Integration_Guide.md, Phase 2 —
"2.3 Regenerate"), producing the working-dir layout upload_to_s3.py expects:

Reads (delivery shape, one dir per collection, collection.json alongside items):
    <raw-dir>/<collection-id>/collection.json
    <raw-dir>/<collection-id>/<item-id>/<item-id>.json

Writes (destination working-dir shape):
    <out-dir>/catalog.json                              (root, mn/nc added as children)
    <out-dir>/program_catalogs/<program>.json            (one per program touched: mn, nc)
    <out-dir>/collections/<collection-id>/collection.json
    <out-dir>/items/<collection-id>/<item-id>/<item-id>.json

Per collection:
    - Add `title: <collection-id>` (dewberry collections don't have one).
    - Normalize `license` to "proprietary" (dewberry ships "other").
    - `stac_version` etc. otherwise left as delivered.

Per item:
    - Rewrite every asset href from the staging prefix to the destination data root,
      and set `s3_key` to the bucket-relative key (ripple1d-pipeline reads this
      directly via boto3 — see stac_importer.py).
    - Normalize the thumbnail asset: rename a capitalized "Thumbnail" key to
      lowercase "thumbnail"; if the media type is sitting inside `roles` instead of a
      `type` field, move it.
    - Upgrade the projection extension: proj:epsg (int) -> proj:code ("EPSG:<int>"),
      and bump the stac_extensions URL from v1.1.0 to v2.0.0. Items with a null
      proj:epsg get no proj:code at all (matches how the authoritative catalog
      represents an unknown CRS) rather than a nonsensical "EPSG:None".
    - Add item-level `fim_group`, derived from the parent collection's
      summaries.fim_groups (stripping a "FIM" prefix, e.g. "FIM30" -> 30). Empty
      summaries.fim_groups -> fim_group: [] (matches existing precedent, e.g.
      ble_no_crs, ble_2D_only, mip_19010102 in the authoritative catalog).
    - Everything else (properties, geometry, bbox, item stac_version, all RAS-file
      assets) is left untouched.

Idempotent: re-running against the same --raw-dir with the same --out-dir overwrites
with identical output. Safe to re-run after fixing a bug and without cleaning --out-dir
first.

`--data-root` is an S3 URI: s3://<bucket> or s3://<bucket>/<prefix>. The script
appends /hec-ras/<collection-id>/<item-id>/<file> after it, matching the convention
rewrite_hrefs.py uses for the existing ble_*/mip_* catalog.

Usage:
    # Pilot: only the 6 selected collections
    python3 generate_nc_mn_catalog.py \
      --raw-dir ~/Desktop/work/nc-mn-migration-work/pilot/raw \
      --out-dir ~/Desktop/work/nc-mn-migration-work/pilot \
      --collections mn_27041,mn_27053,mn_27073,nc_370652011071501,nc_371040200912221,nc_371040201202141 \
      --dry-run

    # Full rollout: everything under --raw-dir
    python3 generate_nc_mn_catalog.py \
      --raw-dir ~/Desktop/work/nc-mn-migration-work/full/raw \
      --out-dir ~/Desktop/work/nc-mn-migration-work/full
"""

import argparse
import copy
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

STAGING_HREF_PREFIX = "s3://fimc-data/dewberry-stac/hv-fim-dev-data"
OLD_PROJECTION_EXT = "https://stac-extensions.github.io/projection/v1.1.0/schema.json"
NEW_PROJECTION_EXT = "https://stac-extensions.github.io/projection/v2.0.0/schema.json"

# Collection-id prefix -> program dir. Mirrors generate_catalog.py / upload_to_s3.py's
# PROGRAMS lists (mn_/nc_ are the two additions this integration makes there).
PROGRAMS: list[tuple[str, str]] = [
    ("mn_", "mn"),
    ("nc_", "nc"),
]

# Existing root-catalog children this script must preserve untouched when it rewrites
# the root catalog.json to add mn/nc.
EXISTING_PROGRAM_HREFS = {"ble", "mip", "ohio_rfc"}


def classify_collection(collection_id: str) -> Optional[str]:
    for prefix, program in PROGRAMS:
        if collection_id.startswith(prefix):
            return program
    return None


def validate_s3_root(uri: str, flag: str) -> str:
    if not uri.startswith("s3://"):
        raise argparse.ArgumentTypeError(f"{flag} must start with 's3://' (got '{uri}')")
    if uri.endswith("/"):
        raise argparse.ArgumentTypeError(f"{flag} must not have a trailing slash (got '{uri}')")
    tail = uri[len("s3://"):]
    if not tail or tail.startswith("/"):
        raise argparse.ArgumentTypeError(f"{flag} must include a bucket after 's3://' (got '{uri}')")
    return uri


def _load_json(path: Path) -> dict[str, Any]:
    with path.open() as fh:
        return json.load(fh)


def _write_json(path: Path, payload: dict[str, Any], dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")


def rewrite_collection(coll: dict[str, Any]) -> dict[str, Any]:
    """Apply collection-level fixes. Returns a new dict; does not mutate the input."""
    out = copy.deepcopy(coll)
    out.setdefault("title", out["id"])
    out["license"] = "proprietary"
    return out


def _rewrite_href(href: str, data_root: str, collection_id: str, item_id: str) -> tuple[str, str]:
    """Return (new_href, s3_key) for one asset href."""
    parsed = urlparse(href)
    if href.startswith(STAGING_HREF_PREFIX):
        rel_path = href[len(STAGING_HREF_PREFIX):].lstrip("/")
    elif parsed.scheme == "https" and "s3" in (parsed.hostname or ""):
        # https://fimc-data.s3.amazonaws.com/dewberry-stac/hv-fim-dev-data/<rel>
        marker = "dewberry-stac/hv-fim-dev-data/"
        idx = parsed.path.find(marker)
        if idx == -1:
            raise ValueError(f"Unrecognized href shape, can't locate data-root marker: {href}")
        rel_path = parsed.path[idx + len(marker):].lstrip("/")
    else:
        raise ValueError(f"Unrecognized href scheme/prefix: {href}")

    # rel_path is "<collection-id>/<item-id>/<file>" in the staging layout; the
    # destination layout is identical in shape, just under hec-ras/ instead of bare.
    new_href = f"{data_root}/hec-ras/{rel_path}"
    # s3_key is bucket-relative (ripple1d-pipeline resolves it against the bucket
    # name alone, not data_root) -- strip "s3://<bucket>/" off new_href rather than
    # hardcoding "hec-ras/", so it includes the data_root's own path segments
    # (e.g. "hv-fim-dev-data/") exactly like existing production items' s3_key.
    s3_key = urlparse(new_href).path.lstrip("/")
    return new_href, s3_key


def _fim_group_for(collection: dict[str, Any]) -> list[int]:
    """Derive item-level fim_group from the collection's summaries.fim_groups.

    "FIM30" -> 30. Empty summaries.fim_groups -> [] (matches existing precedent
    for collections like ble_no_crs / ble_2D_only / mip_19010102).
    """
    raw = collection.get("summaries", {}).get("fim_groups", [])
    result = []
    for entry in raw:
        text = str(entry)
        digits = "".join(ch for ch in text if ch.isdigit())
        if digits:
            result.append(int(digits))
        else:
            logger.warning(f"Could not parse fim_group entry {entry!r}; skipping")
    return result


def rewrite_item(item: dict[str, Any], collection: dict[str, Any], data_root: str) -> dict[str, Any]:
    """Apply item-level fixes. Returns a new dict; does not mutate the input."""
    out = copy.deepcopy(item)
    collection_id = out["collection"]
    item_id = out["id"]

    # --- assets: href + s3_key rewrite, thumbnail normalization ---
    assets = out.get("assets", {})
    new_assets: dict[str, Any] = {}
    for key, asset in assets.items():
        asset = dict(asset)
        href = asset.get("href", "")
        if href:
            new_href, s3_key = _rewrite_href(href, data_root, collection_id, item_id)
            asset["href"] = new_href
            asset["s3_key"] = s3_key

        if key.lower() == "thumbnail":
            key = "thumbnail"
            roles = asset.get("roles", []) or []
            media_roles = [r for r in roles if "/" in r]
            if media_roles:
                asset["type"] = media_roles[0]
                asset["roles"] = [r for r in roles if "/" not in r] or ["thumbnail"]
            asset.setdefault("type", "image/png")
            if asset.get("title", "").lower() == "thumbnail":
                asset["title"] = "thumbnail"

        new_assets[key] = asset
    out["assets"] = new_assets

    # --- projection extension: v1.1.0 (proj:epsg) -> v2.0.0 (proj:code) ---
    exts = out.get("stac_extensions", []) or []
    out["stac_extensions"] = [NEW_PROJECTION_EXT if e == OLD_PROJECTION_EXT else e for e in exts]

    props = out.get("properties", {})
    if "proj:epsg" in props:
        epsg = props.pop("proj:epsg")
        if epsg is not None:
            props["proj:code"] = f"EPSG:{epsg}"
        # epsg is None -> no proj:code at all (matches authoritative items with an
        # unknown CRS; proj:wkt2 still carries the projection).
    out["properties"] = props

    # --- fim_group, derived from the parent collection ---
    out["fim_group"] = _fim_group_for(collection)

    return out


def process_collection(raw_dir: Path, cid: str, out_dir: Path, data_root: str,
                       dry_run: bool) -> tuple[int, int]:
    """Process one collection. Returns (items_written, assets_rewritten)."""
    coll_path = raw_dir / cid / "collection.json"
    if not coll_path.exists():
        raise FileNotFoundError(f"No collection.json for {cid} at {coll_path}")

    raw_coll = _load_json(coll_path)
    new_coll = rewrite_collection(raw_coll)
    _write_json(out_dir / "collections" / cid / "collection.json", new_coll, dry_run)

    items_written = 0
    assets_rewritten = 0
    item_paths = sorted((raw_dir / cid).glob("*/*.json"))
    for item_path in item_paths:
        raw_item = _load_json(item_path)
        new_item = rewrite_item(raw_item, raw_coll, data_root)
        item_id = new_item["id"]
        _write_json(out_dir / "items" / cid / item_id / f"{item_id}.json", new_item, dry_run)
        items_written += 1
        assets_rewritten += len(new_item.get("assets", {}))

    return items_written, assets_rewritten


def write_program_catalogs(out_dir: Path, programs_touched: dict[str, list[str]], dry_run: bool) -> None:
    """Write out-dir/program_catalogs/<program>.json for each program touched."""
    for program, collection_ids in programs_touched.items():
        catalog = {
            "type": "Catalog",
            "id": program,
            "stac_version": "1.0.0",
            "description": f"HEC-RAS models for {program.upper()}",
            "links": [
                {"rel": "child", "href": f"./{cid}/collection.json", "type": "application/json"}
                for cid in sorted(collection_ids)
            ],
        }
        _write_json(out_dir / "program_catalogs" / f"{program}.json", catalog, dry_run)


def write_root_catalog(out_dir: Path, reference_root: Optional[Path], programs_touched: set[str],
                       dry_run: bool) -> None:
    """Write out-dir/catalog.json: the existing root catalog plus mn/nc child links.

    If --reference-root-catalog isn't given, or the referenced file can't be read,
    falls back to a minimal root containing only the programs this run touched (still
    correct for a from-scratch run, but won't preserve ble/mip/ohio_rfc links -- see
    the warning this prints in that case).
    """
    base: dict[str, Any]
    if reference_root and reference_root.exists():
        base = copy.deepcopy(_load_json(reference_root))
    else:
        logger.warning(
            "No usable --reference-root-catalog; writing a root catalog.json containing "
            "ONLY the mn/nc program links from this run. Do not upload this over the live "
            "root catalog.json without first merging in the existing ble/mip/ohio_rfc links."
        )
        base = {
            "type": "Catalog",
            "id": "hec-ras-stac",
            "stac_version": "1.0.0",
            "description": "HEC-RAS STAC catalog.",
            "links": [],
        }

    links = base.get("links", [])
    existing_children = [link for link in links if link.get("rel") == "child"]
    existing_child_ids = {link["href"].rstrip("/").rsplit("/", 1)[-1] for link in existing_children}

    # Match the href STYLE of the existing child links, not a hardcoded relative path.
    # The live root catalog's ble/mip/ohio_rfc children are absolute API URLs
    # (http://<host>:<port>/<program>) generated by stac-fastapi, not relative S3
    # paths -- writing relative hrefs for mn/nc here would make them visibly
    # inconsistent with their siblings in the same links array.
    self_href = next((link["href"] for link in links if link.get("rel") == "self"), None)
    if existing_children:
        sample_href = existing_children[0]["href"]
        api_base = sample_href.rsplit("/", 1)[0]
    elif self_href:
        api_base = self_href.rstrip("/")
    else:
        api_base = None
        logger.warning(
            "No existing child links or self link found in the reference root catalog; "
            "falling back to relative hrefs for mn/nc child links (./<program>/catalog.json). "
            "Verify this matches the live catalog's convention before uploading."
        )

    for program in sorted(programs_touched):
        if program in existing_child_ids:
            continue
        href = f"{api_base}/{program}" if api_base else f"./{program}/catalog.json"
        links.append({"rel": "child", "href": href, "type": "application/json"})
    base["links"] = links

    _write_json(out_dir / "catalog.json", base, dry_run)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate catalog.json/collection.json/items for a state-portal STAC delivery")
    parser.add_argument("--raw-dir", required=True, type=Path,
                        help="Delivery dir: <raw-dir>/<collection-id>/collection.json + items")
    parser.add_argument("--out-dir", required=True, type=Path,
                        help="Working-dir output, in the shape upload_to_s3.py expects")
    parser.add_argument("--data-root", default="s3://hv-fim-dev-data",
                        type=lambda v: validate_s3_root(v, "--data-root"),
                        help="Destination data root S3 URI (default: s3://hv-fim-dev-data)")
    parser.add_argument("--collections", default=None,
                        help="Comma-separated collection-id filter. Omit to process every "
                             "collection found under --raw-dir.")
    parser.add_argument("--reference-root-catalog", type=Path, default=None,
                        help="Existing root catalog.json to add mn/nc children to (so ble/mip/"
                             "ohio_rfc links are preserved). Typically the synced "
                             "hec-ras-stac/catalog.json mirror.")
    parser.add_argument("--dry-run", action="store_true", help="Report planned changes, write nothing")
    args = parser.parse_args()

    raw_dir = args.raw_dir.expanduser()
    out_dir = args.out_dir.expanduser()

    if not raw_dir.exists():
        logger.error(f"--raw-dir not found: {raw_dir}")
        return 1

    all_collection_ids = sorted(p.name for p in raw_dir.iterdir()
                                if p.is_dir() and (p / "collection.json").exists())
    if args.collections:
        wanted = set(c.strip() for c in args.collections.split(","))
        missing = wanted - set(all_collection_ids)
        if missing:
            logger.error(f"--collections named IDs not found under {raw_dir}: {sorted(missing)}")
            return 1
        collection_ids = [c for c in all_collection_ids if c in wanted]
    else:
        collection_ids = all_collection_ids

    if not collection_ids:
        logger.error(f"No collections to process under {raw_dir}")
        return 1

    unmatched = [c for c in collection_ids if classify_collection(c) is None]
    if unmatched:
        logger.error(f"Collection ID(s) don't match any known program prefix {[p for p, _ in PROGRAMS]}: "
                    f"{unmatched}")
        return 1

    logger.info(f"{'[DRY RUN] ' if args.dry_run else ''}Processing {len(collection_ids)} "
               f"collection(s) from {raw_dir}")

    programs_touched: dict[str, list[str]] = {}
    total_items = 0
    total_assets = 0
    for cid in collection_ids:
        program = classify_collection(cid)
        programs_touched.setdefault(program, []).append(cid)
        items_written, assets_rewritten = process_collection(raw_dir, cid, out_dir, args.data_root, args.dry_run)
        total_items += items_written
        total_assets += assets_rewritten
        logger.info(f"  {cid}: {items_written} item(s), {assets_rewritten} asset(s) rewritten")

    write_program_catalogs(out_dir, programs_touched, args.dry_run)
    write_root_catalog(out_dir, args.reference_root_catalog, set(programs_touched), args.dry_run)

    logger.info(f"{'[DRY RUN] would write' if args.dry_run else 'Wrote'}: "
               f"{len(collection_ids)} collection(s), {total_items} item(s), "
               f"{total_assets} asset(s) rewritten, "
               f"{len(programs_touched)} program catalog(s) ({sorted(programs_touched)}), "
               f"1 root catalog.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
