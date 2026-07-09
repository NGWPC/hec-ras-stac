"""
Rewrite S3 asset HREFs in a local STAC catalog directory.

Run this after syncing the catalog from the NGWPC source bucket and before
re-syncing to the OWP serving bucket. It rewrites every asset `href` that
references the NGWPC source path to use the OWP bucket path instead, producing
an accurate static catalog that can be loaded into pgSTAC without any
post-load DB patching.

Both `href` and `s3_key` are rewritten. `s3_key` is a bucket-relative key, so it
is rebased onto the destination bucket alongside the href — it is set to whatever
follows `s3://<dest-prefix>/` in the new href. Without this, direct boto3 access
(`Bucket=hv-fim-dev-data, Key=<s3_key>`) would 404 on the OWP deployment.

Default substitution:
  href:   s3://fimc-data/hv-fim-dev-data/hec-ras/... → s3://hv-fim-dev-data/hec-ras/...
  s3_key:           hv-fim-dev-data/hec-ras/...      →                    hec-ras/...

After running this script, re-sync the corrected JSONs to S3:
  aws s3 sync <catalog_dir>/ s3://hv-fim-dev-stac/hec-ras-stac/

Usage:
    # Dry run — report counts, no files written
    python3 rewrite_catalog_hrefs.py ~/hec-ras-catalog --dry-run

    # Apply in-place
    python3 rewrite_catalog_hrefs.py ~/hec-ras-catalog

    # Custom substitution
    python3 rewrite_catalog_hrefs.py ~/hec-ras-catalog \\
      --source-prefix fimc-data/hv-fim-dev-data \\
      --dest-prefix hv-fim-dev-data
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any


DEFAULT_SOURCE_PREFIX = "fimc-data/hv-fim-dev-data"
DEFAULT_DEST_PREFIX = "hv-fim-dev-data"


def _rewrite_assets(assets: dict[str, Any], source: str, dest: str) -> tuple[dict[str, Any], int]:
    """Rewrite href and s3_key fields in an assets dict. Returns (updated_assets, rewrite_count)."""
    count = 0
    for asset_data in assets.values():
        href = asset_data.get("href", "")
        if href.startswith(f"s3://{source}/"):
            key = href[len(f"s3://{source}/"):]
            asset_data["href"] = f"s3://{dest}/{key}"
            asset_data["s3_key"] = key
            count += 1
    return assets, count


def _process_file(path: Path, source: str, dest: str, dry_run: bool) -> int:
    """Rewrite hrefs in a single JSON file. Returns number of hrefs rewritten."""
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"  WARNING: skipping {path}: {e}")
        return 0

    assets = data.get("assets")
    if not assets:
        return 0

    _, count = _rewrite_assets(assets, source, dest)
    if count and not dry_run:
        path.write_text(json.dumps(data, separators=(",", ":")))

    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Rewrite S3 asset HREFs in a local STAC catalog directory")
    parser.add_argument("catalog_dir", help="Local catalog directory (output of aws s3 sync)")
    parser.add_argument("--source-prefix", default=DEFAULT_SOURCE_PREFIX,
                        help=f"S3 path prefix to replace (default: {DEFAULT_SOURCE_PREFIX})")
    parser.add_argument("--dest-prefix",   default=DEFAULT_DEST_PREFIX,
                        help=f"Replacement S3 path prefix (default: {DEFAULT_DEST_PREFIX})")
    parser.add_argument("--dry-run",       action="store_true")
    args = parser.parse_args()

    catalog_dir = Path(args.catalog_dir)
    if not catalog_dir.is_dir():
        print(f"ERROR: {catalog_dir} is not a directory")
        return 1

    print("=" * 70)
    print("HEC-RAS STAC — Catalog HREF Rewriter")
    print("=" * 70)
    print(f"Catalog dir:  {catalog_dir}")
    print(f"Substitution: s3://{args.source_prefix}/... → s3://{args.dest_prefix}/...")
    if args.dry_run:
        print("MODE: DRY RUN")
    print()

    json_files = list(catalog_dir.rglob("*.json"))
    print(f"Scanning {len(json_files)} JSON files...")

    files_updated = 0
    hrefs_rewritten = 0

    for path in json_files:
        count = _process_file(path, args.source_prefix, args.dest_prefix, args.dry_run)
        if count:
            files_updated += 1
            hrefs_rewritten += count

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Files {'would be ' if args.dry_run else ''}updated: {files_updated}")
    print(f"HREFs {'would be ' if args.dry_run else ''}rewritten: {hrefs_rewritten}")
    if args.dry_run:
        print("\n[DRY RUN] No files written")
    else:
        print(f"\nNext: sync corrected catalog to S3:")
        print(f"  aws s3 sync {catalog_dir}/ s3://hv-fim-dev-stac/hec-ras-stac/")

    return 0


if __name__ == "__main__":
    sys.exit(main())
