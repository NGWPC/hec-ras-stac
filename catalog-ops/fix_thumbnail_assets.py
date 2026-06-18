"""
Fix thumbnail asset structure in local STAC catalog item JSONs.

The HEC-RAS catalog was generated with a non-compliant thumbnail asset structure:
  - Key is "Thumbnail" (capital T) instead of "thumbnail"
  - "image/png" is in the roles array instead of a separate "type" field

This script rewrites item JSONs to match the STAC spec and STAC Browser expectations:

  Before:
    "Thumbnail": {"href": "...", "roles": ["thumbnail", "image/png"], ...}

  After:
    "thumbnail": {"href": "...", "type": "image/png", "roles": ["thumbnail"], ...}

Run on the local catalog directory after syncing from S3, then re-sync to S3.
Only item JSONs are affected (collection.json and catalog.json have no assets).

Usage:
    # Dry run
    python3 fix_thumbnail_assets.py ~/hec-ras-catalog --dry-run

    # Apply
    python3 fix_thumbnail_assets.py ~/hec-ras-catalog

    # Then re-sync to S3
    aws s3 sync ~/hec-ras-catalog/ s3://<your-stac-bucket>/hec-ras-stac/
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional


def _find_thumbnail_key(assets: dict) -> Optional[str]:
    """Find the asset key with thumbnail role, regardless of key name."""
    for key in ("Thumbnail", "thumbnail"):
        if key in assets:
            return key
    for key, asset in assets.items():
        if "thumbnail" in asset.get("roles", []):
            return key
    return None


def _fix_thumbnail(assets: dict) -> tuple[dict, bool]:
    """Fix thumbnail asset structure. Returns (updated_assets, changed)."""
    key = _find_thumbnail_key(assets)
    if not key:
        return assets, False

    thumbnail = dict(assets[key])
    roles = thumbnail.get("roles", [])

    # Nothing to fix if already correct
    if key == "thumbnail" and "image/png" not in roles and "type" in thumbnail:
        return assets, False

    thumbnail["roles"] = [r for r in roles if r != "image/png"]
    thumbnail["type"] = "image/png"

    assets = {k: v for k, v in assets.items() if k != key}
    assets["thumbnail"] = thumbnail

    return assets, True


def _process_file(path: Path, dry_run: bool) -> bool:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"  WARNING: skipping {path}: {e}")
        return False

    assets = data.get("assets")
    if not assets or not _find_thumbnail_key(assets):
        return False

    data["assets"], _ = _fix_thumbnail(assets)

    if not dry_run:
        path.write_text(json.dumps(data, separators=(",", ":")))

    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Fix thumbnail asset structure in STAC catalog item JSONs")
    parser.add_argument("catalog_dir", help="Local catalog directory (output of aws s3 sync)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    catalog_dir = Path(args.catalog_dir)
    if not catalog_dir.is_dir():
        print(f"ERROR: {catalog_dir} is not a directory")
        return 1

    print("=" * 70)
    print("HEC-RAS STAC — Thumbnail Asset Fixer")
    print("=" * 70)
    print(f"Catalog dir: {catalog_dir}")
    if args.dry_run:
        print("MODE: DRY RUN")
    print()

    json_files = list(catalog_dir.rglob("*.json"))
    print(f"Scanning {len(json_files)} JSON files...")

    files_updated = 0
    for path in json_files:
        if _process_file(path, args.dry_run):
            files_updated += 1

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Files {'would be ' if args.dry_run else ''}updated: {files_updated}")
    if args.dry_run:
        print("\n[DRY RUN] No files written")
    else:
        print(f"\nNext: sync corrected catalog to S3:")
        print(f"  aws s3 sync {catalog_dir}/ s3://<your-stac-bucket>/hec-ras-stac/")

    return 0


if __name__ == "__main__":
    sys.exit(main())
