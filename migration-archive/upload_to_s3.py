"""
Upload the local STAC tree to the destination STAC root.

Reads (local layout, produced by generate_collections.py + generate_catalog.py
+ rewrite_hrefs.py):
    <working_dir>/catalog.json
    <working_dir>/program_catalogs/<program>.json     (one per program: ble/mip/ohio_rfc)
    <working_dir>/collections/<collection-id>/collection.json
    <working_dir>/items/<collection-id>/<item-id>/<item-id>.json

Writes (destination layout — per-program parent Catalogs):
    <stac-root>/hec-ras-stac/catalog.json
    <stac-root>/hec-ras-stac/<program>/catalog.json
    <stac-root>/hec-ras-stac/<program>/<collection-id>/collection.json
    <stac-root>/hec-ras-stac/<program>/<collection-id>/<item-id>/<item-id>.json

Program assignment is by collection-id prefix (see generate_catalog.py for
the rule). Collections whose id doesn't match any program prefix are logged
and skipped.

`--stac-root` is an S3 URI: `s3://<bucket>` (production) or
`s3://<bucket>/<key-prefix>` (multi-tenant / test).

The data root (assets) is populated separately by sync_assets.py direct
S3-to-S3 — no local copy involved. Data bucket layout stays FLAT (no
per-program subdir there):
    <data-root>/hec-ras/<collection-id>/<item-id>/<asset-files>

Prerequisite: `rewrite_hrefs.py` must have restructured the local items dir
from source shape into destination shape
(items/<collection-id>/<item-id>/<item-id>.json).

Usage:
    python upload_to_s3.py --stac-root s3://hv-fim-dev-stac --dest-profile <profile>
    python upload_to_s3.py --stac-root s3://fimc-data/test-hv-fim-dev-stac ...
    python upload_to_s3.py ... --dry-run
"""

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Collection-id prefix → program-dir. Mirrors generate_catalog.py's PROGRAMS.
PROGRAMS = [
    ("ble_", "ble"),
    ("mip_", "mip"),
    ("ohio_rfc", "ohio_rfc"),
    ("mn_", "mn"),
    ("nc_", "nc"),
]


def classify_collection(collection_id: str) -> Optional[str]:
    """Return the program dir for a collection id, or None if it doesn't match any."""
    for prefix, program in PROGRAMS:
        if collection_id.startswith(prefix):
            return program
    return None


def _aws_env_dest() -> dict:
    """Return env with DEST_AWS_* mapped to AWS_* if set; else use existing AWS_* unchanged."""
    env = os.environ.copy()
    if any(env.get(f"DEST_AWS_{k}") for k in ("ACCESS_KEY_ID", "SECRET_ACCESS_KEY")):
        for k in ("ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "SESSION_TOKEN"):
            v = env.get(f"DEST_AWS_{k}")
            if v is not None:
                env[f"AWS_{k}"] = v
            elif k == "SESSION_TOKEN":
                env.pop("AWS_SESSION_TOKEN", None)
    return env


def validate_s3_root(uri: str, flag: str) -> str:
    """Validate an S3 root URI: s3://bucket[/prefix], no trailing slash."""
    if not uri.startswith("s3://"):
        raise argparse.ArgumentTypeError(f"{flag} must start with 's3://' (got '{uri}')")
    if uri.endswith("/"):
        raise argparse.ArgumentTypeError(f"{flag} must not have a trailing slash (got '{uri}')")
    tail = uri[len("s3://"):]
    if not tail or tail.startswith("/"):
        raise argparse.ArgumentTypeError(f"{flag} must include a bucket after 's3://' (got '{uri}')")
    return uri


def s3_cp(src: Path, dst: str, profile: Optional[str], dry_run: bool) -> int:
    cmd = ["aws", "s3", "cp", src.as_posix(), dst]
    if profile:
        cmd += ["--profile", profile]
    if dry_run:
        logger.info(f"[DRY RUN] {' '.join(cmd)}")
        return 0
    return subprocess.run(cmd, env=_aws_env_dest()).returncode


def s3_sync(src: Path, dst: str, profile: Optional[str], dry_run: bool) -> int:
    cmd = ["aws", "s3", "sync", src.as_posix() + "/", dst]
    if profile:
        cmd += ["--profile", profile]
    if dry_run:
        logger.info(f"[DRY RUN] {' '.join(cmd)}")
        return 0
    return subprocess.run(cmd, env=_aws_env_dest()).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload local STAC tree to destination STAC root")
    parser.add_argument("--working-dir", default=os.environ.get("WORKING_DIR", "~/ras-stac-migration"))
    parser.add_argument(
        "--stac-root",
        required=True,
        type=lambda v: validate_s3_root(v, "--stac-root"),
        help="Destination STAC root S3 URI, e.g. s3://hv-fim-dev-stac or s3://fimc-data/test-hv-fim-dev-stac",
    )
    parser.add_argument("--dest-profile", default=None, help="AWS profile for destination writes")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    working_dir = Path(args.working_dir).expanduser()
    catalog = working_dir / "catalog.json"
    program_catalogs_dir = working_dir / "program_catalogs"
    collections_dir = working_dir / "collections"
    items_dir = working_dir / "items"

    missing = [p for p in (catalog, program_catalogs_dir, collections_dir, items_dir) if not p.exists()]
    if missing:
        logger.error(f"Missing required input(s) in {working_dir}: {[str(p) for p in missing]}")
        return 1

    base = f"{args.stac_root}/hec-ras-stac"

    # 1. Root catalog
    logger.info(f"Uploading root catalog → {base}/catalog.json")
    rc = s3_cp(catalog, f"{base}/catalog.json", args.dest_profile, args.dry_run)
    if rc != 0:
        return rc

    # 2. Program catalogs
    program_catalog_files = sorted(program_catalogs_dir.glob("*.json"))
    logger.info(f"Uploading {len(program_catalog_files)} program catalog(s)")
    for pcat in program_catalog_files:
        program = pcat.stem
        dest = f"{base}/{program}/catalog.json"
        rc = s3_cp(pcat, dest, args.dest_profile, args.dry_run)
        if rc != 0:
            return rc

    # 3. Per-collection: collection.json + items/<collection>/ subtree
    #    → <base>/<program>/<collection>/
    collection_ids = sorted(p.parent.name for p in collections_dir.glob("*/collection.json"))
    logger.info(f"Uploading {len(collection_ids)} collection(s) under program subdirs")
    unmatched: list[str] = []
    for cid in collection_ids:
        program = classify_collection(cid)
        if program is None:
            unmatched.append(cid)
            continue
        dest_prefix = f"{base}/{program}/{cid}"
        local_collection_json = collections_dir / cid / "collection.json"
        rc = s3_cp(local_collection_json, f"{dest_prefix}/collection.json", args.dest_profile, args.dry_run)
        if rc != 0:
            return rc

        local_items = items_dir / cid
        if local_items.exists():
            rc = s3_sync(local_items, f"{dest_prefix}/", args.dest_profile, args.dry_run)
            if rc != 0:
                return rc
        else:
            logger.info(f"  (no local items for {cid} — empty collection)")

    if unmatched:
        logger.warning(f"{len(unmatched)} collection(s) did not match any program prefix and were NOT uploaded: {unmatched}")

    logger.info("Upload complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
