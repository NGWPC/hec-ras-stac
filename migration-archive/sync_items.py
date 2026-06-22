"""
Sync STAC item JSONs from Dewberry source to a local working dir.

Pulls JSONs only — thumbnails and gpkgs are synced separately by sync_assets.py
direct to the data bucket (STAC bucket = pure metadata).

Source layout:
    s3://fimc-data/dewberry-stac/<prefix>/stac_items/<hash>/<item-id>.json (+ thumbnail.png + .gpkg, skipped)

Local layout (flattened, stac_items/ dropped):
    <working_dir>/items/<prefix>/<hash>/<item-id>.json

The hash is preserved to differentiate items with shared IDs (Dewberry source
has ~20k id collisions across hashes).

Three sync modes (mutually exclusive):
    Default (per-hash loop): one `aws s3 sync` per hash directory. Slow at
      full scale (~30k hash dirs × ~1s CLI startup = hours of overhead). Kept
      because it pairs cleanly with --subset for subset validation.
    --whole-tree: one `aws s3 sync` per prefix, recursive. Drops the
      per-hash CLI overhead. Use for full-scale (178k items) syncs — finishes
      in minutes instead of hours.
    --items-from FILE: per-hash loop driven by a reproducible list of
      <prefix>/<hash_dir> lines from a text file. Used for the curated
      subset (see subset_items.txt) that exercises every code path.

Usage:
    # Per-hash, optionally with subset
    python sync_items.py --source-profile <profile>
    python sync_items.py --source-profile <profile> --subset 10
    python sync_items.py --source-profile <profile> --dry-run

    # Full-tree, fast (full scale)
    python sync_items.py --source-profile <profile> --whole-tree

    # Hand-curated subset (reproducible across machines)
    python sync_items.py --source-profile <profile> --items-from subset_items.txt
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

SOURCE_BUCKET = "fimc-data"
SOURCE_PREFIX = "dewberry-stac"
PREFIXES = ["ebfe", "mip_30", "mip_70"]


def _aws_env_source() -> dict:
    """Return env with SOURCE_AWS_* mapped to AWS_* if set; else use existing AWS_* unchanged."""
    env = os.environ.copy()
    if any(env.get(f"SOURCE_AWS_{k}") for k in ("ACCESS_KEY_ID", "SECRET_ACCESS_KEY")):
        for k in ("ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "SESSION_TOKEN"):
            v = env.get(f"SOURCE_AWS_{k}")
            if v is not None:
                env[f"AWS_{k}"] = v
            elif k == "SESSION_TOKEN":
                env.pop("AWS_SESSION_TOKEN", None)
    return env


def list_hash_dirs(prefix: str, profile: Optional[str]) -> list[str]:
    """List hash directories under <prefix>/stac_items/ in the source bucket."""
    src = f"s3://{SOURCE_BUCKET}/{SOURCE_PREFIX}/{prefix}/stac_items/"
    cmd = ["aws", "s3", "ls", src]
    if profile:
        cmd += ["--profile", profile]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, env=_aws_env_source())
    return sorted(line.split()[-1].rstrip("/") for line in result.stdout.splitlines() if line.strip().startswith("PRE"))


def sync_hash(prefix: str, hash_dir: str, working_dir: Path, profile: Optional[str], dry_run: bool) -> int:
    """Sync one hash dir from source to local, flattening stac_items/ out of the path."""
    src = f"s3://{SOURCE_BUCKET}/{SOURCE_PREFIX}/{prefix}/stac_items/{hash_dir}/"
    dst = working_dir / "items" / prefix / hash_dir
    cmd = ["aws", "s3", "sync", src, str(dst) + "/", "--exclude", "*", "--include", "*.json"]
    if profile:
        cmd += ["--profile", profile]
    if dry_run:
        logger.info(f"[DRY RUN] {' '.join(cmd)}")
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    return subprocess.run(cmd, env=_aws_env_source()).returncode


def sync_prefix_whole(prefix: str, working_dir: Path, profile: Optional[str], dry_run: bool) -> int:
    """Sync an entire prefix's stac_items/ tree in one recursive call.

    The source tree already has the <hash>/<item-id>.json shape under
    stac_items/, so syncing the whole `stac_items/` dir to <working_dir>/items/<prefix>/
    yields the same local layout as the per-hash loop would produce.
    """
    src = f"s3://{SOURCE_BUCKET}/{SOURCE_PREFIX}/{prefix}/stac_items/"
    dst = working_dir / "items" / prefix
    cmd = ["aws", "s3", "sync", src, str(dst) + "/", "--exclude", "*", "--include", "*.json"]
    if profile:
        cmd += ["--profile", profile]
    if dry_run:
        logger.info(f"[DRY RUN] {' '.join(cmd)}")
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    return subprocess.run(cmd, env=_aws_env_source()).returncode


def _parse_items_from(items_from: Path) -> list[tuple[str, str]]:
    """Parse a subset_items.txt file: yields (prefix, hash_dir) pairs.

    Format: one `<prefix>/<hash_dir>` per non-blank, non-comment line.
    Comments start with `#` (whole-line or after content).
    """
    pairs: list[tuple[str, str]] = []
    for lineno, raw in enumerate(items_from.read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"{items_from}:{lineno}: expected '<prefix>/<hash_dir>', got {raw!r}")
        pairs.append((parts[0], parts[1]))
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync STAC item JSONs from Dewberry source (flattens stac_items/)")
    parser.add_argument("--working-dir", default=os.environ.get("WORKING_DIR", "~/ras-stac-migration"), help="Local working directory")
    parser.add_argument("--source-profile", default=None, help="AWS profile for source reads")
    parser.add_argument("--prefixes", nargs="+", default=PREFIXES, help="Source prefixes to sync")
    parser.add_argument("--subset", type=int, default=None, help="Limit to first N hash dirs per prefix (per-hash mode only)")
    parser.add_argument(
        "--whole-tree",
        action="store_true",
        help="Sync each prefix's stac_items/ tree in one recursive call. "
             "Much faster for full-scale syncs; incompatible with --subset/--items-from.",
    )
    parser.add_argument(
        "--items-from",
        type=lambda p: Path(p).expanduser(),
        default=None,
        help="Path to a curated subset file (e.g. subset_items.txt). One '<prefix>/<hash_dir>' per line; "
             "'#' for comments. Reproducible subset that exercises every code path. "
             "Mutually exclusive with --subset and --whole-tree.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    mode_flags = [bool(args.whole_tree), args.subset is not None, args.items_from is not None]
    if sum(mode_flags) > 1:
        parser.error("--whole-tree, --subset, and --items-from are mutually exclusive.")

    working_dir = Path(args.working_dir).expanduser()
    working_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Source: s3://{SOURCE_BUCKET}/{SOURCE_PREFIX}/")
    logger.info(f"Dest:   {working_dir}/items/")
    if args.items_from:
        mode = f"items-from {args.items_from}"
    elif args.whole_tree:
        mode = "whole-tree (one sync per prefix)"
    else:
        mode = "per-hash loop"
    logger.info(f"Mode:   {mode}")

    if args.items_from:
        if not args.items_from.exists():
            logger.error(f"Items list not found: {args.items_from}")
            return 1
        pairs = _parse_items_from(args.items_from)
        logger.info(f"{len(pairs)} (prefix, hash_dir) pairs from {args.items_from}")
        for prefix, hash_dir in pairs:
            rc = sync_hash(prefix, hash_dir, working_dir, args.source_profile, args.dry_run)
            if rc != 0:
                logger.error(f"Sync failed for {prefix}/{hash_dir}")
                return rc
        logger.info("Item sync complete.")
        return 0

    for prefix in args.prefixes:
        if args.whole_tree:
            logger.info(f"{prefix}: syncing entire stac_items/ tree ...")
            rc = sync_prefix_whole(prefix, working_dir, args.source_profile, args.dry_run)
            if rc != 0:
                logger.error(f"Sync failed for {prefix}")
                return rc
            continue

        logger.info(f"Listing hash dirs for {prefix}...")
        hashes = list_hash_dirs(prefix, args.source_profile)
        if args.subset:
            hashes = hashes[:args.subset]
        logger.info(f"{prefix}: {len(hashes)} hash dirs to sync")

        for hash_dir in hashes:
            rc = sync_hash(prefix, hash_dir, working_dir, args.source_profile, args.dry_run)
            if rc != 0:
                logger.error(f"Sync failed for {prefix}/{hash_dir}")
                return rc

    logger.info("Item sync complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
