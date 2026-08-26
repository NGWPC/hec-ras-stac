"""
Generate the root catalog.json and per-program catalog.json files from the
local collection.json files.

Reads:  <working_dir>/collections/<collection-id>/collection.json
Writes: <working_dir>/catalog.json                          ← root
        <working_dir>/program_catalogs/<program>.json       ← one per program

Program assignment is by collection-id prefix:
    ble_*    → ble
    mip_*    → mip
    ohio_rfc → ohio_rfc
    anything else           → uncategorized warning

The root catalog links to the program catalogs as STAC `child` entries;
each program catalog links to its collections. upload_to_s3.py knows where
each piece lands on S3:

    <stac-root>/hec-ras-stac/catalog.json
    <stac-root>/hec-ras-stac/<program>/catalog.json
    <stac-root>/hec-ras-stac/<program>/<collection-id>/...

Usage:
    python generate_catalog.py --new-stac-url http://hec-ras-stac:8082
    python generate_catalog.py --new-stac-url http://hec-ras-stac:8082 --dry-run
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Program-prefix → program-dir mapping. Order matters only for fallthrough
# (ohio_rfc must be checked before ohio_* in case of future expansion, but
# right now ohio_rfc is the only ohio collection).
PROGRAMS = [
    ("ble_", "ble"),
    ("mip_", "mip"),
    ("ohio_rfc", "ohio_rfc"),
    ("mn_", "mn"),
    ("nc_", "nc"),
]


def classify_collection(collection_id: str) -> str | None:
    """Return the program dir for a collection id, or None if it doesn't match any."""
    for prefix, program in PROGRAMS:
        if collection_id.startswith(prefix):
            return program
    return None


def make_program_catalog(program: str, api_base: str, collection_ids: list[str]) -> dict:
    """Build the STAC Catalog dict for one program."""
    return {
        "type": "Catalog",
        "id": f"hec-ras-stac-{program}",
        "stac_version": "1.0.0",
        "description": f"HEC-RAS STAC catalog — {program.upper()} program collections.",
        "links": [
            {"rel": "self", "href": f"{api_base}/{program}", "type": "application/json"},
            {"rel": "parent", "href": api_base, "type": "application/json"},
            {"rel": "root", "href": api_base, "type": "application/json"},
        ] + [
            {"rel": "child", "href": f"{api_base}/collections/{cid}", "type": "application/json"}
            for cid in collection_ids
        ],
    }


def make_root_catalog(api_base: str, programs: list[str]) -> dict:
    """Build the root STAC Catalog dict; children are program catalogs."""
    return {
        "type": "Catalog",
        "id": "hec-ras-stac",
        "stac_version": "1.0.0",
        "description": "HEC-RAS STAC catalog — BLE and MIP HEC-RAS models from Dewberry, plus regional programs.",
        "links": [
            {"rel": "self", "href": api_base, "type": "application/json"},
        ] + [
            {"rel": "child", "href": f"{api_base}/{program}", "type": "application/json"}
            for program in programs
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Write root + per-program catalog.json files")
    parser.add_argument("--working-dir", default=os.environ.get("WORKING_DIR", "~/ras-stac-migration"))
    parser.add_argument("--new-stac-url", required=True, help="Destination STAC API base URL (e.g. http://hec-ras-stac:8082)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    working_dir = Path(args.working_dir).expanduser()
    collections_dir = working_dir / "collections"
    if not collections_dir.exists():
        logger.error(f"Collections dir not found: {collections_dir} — run generate_collections.py first")
        return 1

    collection_ids = sorted(p.parent.name for p in collections_dir.glob("*/collection.json"))
    logger.info(f"Found {len(collection_ids)} collections")

    # Group by program
    by_program: dict[str, list[str]] = {program: [] for _, program in PROGRAMS}
    uncategorized: list[str] = []
    for cid in collection_ids:
        program = classify_collection(cid)
        if program is None:
            uncategorized.append(cid)
        else:
            by_program[program].append(cid)

    if uncategorized:
        logger.warning(f"{len(uncategorized)} collections did not match any program prefix "
                       f"and will not appear in any program catalog: {uncategorized}")

    programs_to_emit = [p for _, p in PROGRAMS if by_program[p]]
    for program in programs_to_emit:
        logger.info(f"  {program}: {len(by_program[program])} collection(s)")

    api_base = args.new_stac_url.rstrip("/")
    root_catalog = make_root_catalog(api_base, programs_to_emit)
    program_catalogs = {
        program: make_program_catalog(program, api_base, sorted(by_program[program]))
        for program in programs_to_emit
    }

    root_out = working_dir / "catalog.json"
    program_dir = working_dir / "program_catalogs"

    if args.dry_run:
        logger.info(f"[DRY RUN] Would write {root_out} with {len(programs_to_emit)} child links")
        for program in programs_to_emit:
            logger.info(f"[DRY RUN] Would write {program_dir}/{program}.json "
                        f"with {len(by_program[program])} child links")
        return 0

    root_out.write_text(json.dumps(root_catalog, indent=2))
    logger.info(f"Wrote {root_out}")

    program_dir.mkdir(parents=True, exist_ok=True)
    for program in programs_to_emit:
        out = program_dir / f"{program}.json"
        out.write_text(json.dumps(program_catalogs[program], indent=2))
        logger.info(f"Wrote {out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
