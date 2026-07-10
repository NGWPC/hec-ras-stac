"""
Rewrite S3 asset HREFs in a live pgSTAC database to use the asset-proxy service.

Run this after load_catalog.py to make private S3 assets accessible via browser
through the asset-proxy (port 8083).

Only the `href` field on each asset is rewritten. This script leaves `s3_key`
untouched — it holds the real S3 key for direct boto3 access, and was already
rebased onto the destination bucket upstream by rewrite_catalog_hrefs.py.

Uses a single SQL UPDATE — no per-row Python loop. Idempotent: re-running when
nothing needs rewriting prints "Nothing to do."

Assumes asset HREFs in the catalog are already correct s3:// URIs pointing at
the bucket the EC2 instance role can access. For OWP deployments, run
rewrite_catalog_hrefs.py on the local catalog before loading to ensure HREFs
reference the OWP bucket, not the NGWPC source.

Usage:
    export HOST_IP=$(hostname -I | awk '{print $1}')
    export PGPASSWORD=$(sudo cat /opt/hec-ras-stac/.db_password)

    # Dry run
    sudo -E python3 rewrite_asset_urls.py --proxy-url http://$HOST_IP:8083 --dry-run

    # Apply
    sudo -E python3 rewrite_asset_urls.py --proxy-url http://$HOST_IP:8083

    # Batch mode + skip thumbnails (recommended — avoids lock errors and proxy load from thumbnails)
    sudo -E python3 rewrite_asset_urls.py --proxy-url http://$HOST_IP:8083 --batch --skip-thumbnails

    # Scope to a single collection prefix
    sudo -E python3 rewrite_asset_urls.py --proxy-url http://$HOST_IP:8083 --collection-prefix mip
"""

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional

import psycopg2


DB_PASSWORD_FILE = "/opt/hec-ras-stac/.db_password"
BATCH_PREFIXES = ["ble_", "mip_", "ohio_rfc"]


def _build_sql(proxy_base: str, collection_prefix: Optional[str], skip_thumbnails: bool = False) -> tuple[str, str, list]:
    proxy = proxy_base.rstrip("/")

    params: list = []
    collection_filter = ""
    if collection_prefix:
        collection_filter = "AND collection LIKE %s"
        params.append(f"{collection_prefix}%")

    thumbnail_filter = "AND key != 'thumbnail'" if skip_thumbnails else ""

    update_sql = f"""
        UPDATE pgstac.items
        SET content = jsonb_set(
            content,
            '{{assets}}',
            (
                SELECT jsonb_object_agg(
                    key,
                    CASE
                        WHEN value->>'href' LIKE 's3://%%' {thumbnail_filter}
                        THEN jsonb_set(value, '{{href}}', to_jsonb(
                            '{proxy}/s3/' || substring(value->>'href' FROM length('s3://') + 1)
                        ))
                        ELSE value
                    END
                )
                FROM jsonb_each(content->'assets')
            )
        )
        WHERE content->'assets' IS NOT NULL
          AND (content->'assets')::text LIKE '%%s3://%%'
          {collection_filter}
    """

    count_sql = f"""
        SELECT COUNT(*) FROM pgstac.items
        WHERE content->'assets' IS NOT NULL
          AND (content->'assets')::text LIKE '%%s3://%%'
          {collection_filter}
    """

    return update_sql, count_sql, params


def _resolve_password(args: argparse.Namespace) -> Optional[str]:
    if args.db_password:
        return args.db_password
    if pw := os.environ.get("PGPASSWORD"):
        return pw
    pw_file = Path(DB_PASSWORD_FILE)
    if pw_file.exists():
        return pw_file.read_text().strip()
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Rewrite S3 asset HREFs to asset-proxy URLs in pgSTAC")
    parser.add_argument("--proxy-url",         required=True, help="Asset proxy base URL (e.g. http://localhost:8083)")
    parser.add_argument("--collection-prefix", default=None,  help="Only process collections with this prefix (e.g. mip, ble)")
    parser.add_argument("--db-host",           default=os.environ.get("PGHOST", "localhost"))
    parser.add_argument("--db-port",           type=int, default=5432)
    parser.add_argument("--db-user",           default="pgstac")
    parser.add_argument("--db-password",       default=None,  help=f"Or set PGPASSWORD, or place in {DB_PASSWORD_FILE}")
    parser.add_argument("--db-name",           default="stacdb")
    parser.add_argument("--dry-run",           action="store_true")
    parser.add_argument("--skip-thumbnails",   action="store_true",
                        help="Leave thumbnail asset HREFs as s3:// (avoids proxying thumbnails, improves browser load time)")
    parser.add_argument("--batch",             action="store_true",
                        help=f"Process collections in separate transactions by prefix {BATCH_PREFIXES} to avoid lock exhaustion")
    args = parser.parse_args()

    password = _resolve_password(args)
    if password is None:
        print(f"ERROR: database password required — set --db-password, PGPASSWORD, or {DB_PASSWORD_FILE}")
        return 1

    print("=" * 70)
    print("HEC-RAS STAC — Asset URL Rewriter")
    print("=" * 70)
    print(f"Proxy URL: {args.proxy_url}")
    print(f"Database:  {args.db_host}:{args.db_port}/{args.db_name}")
    if args.collection_prefix:
        print(f"Collection: {args.collection_prefix}*")
    if args.dry_run:
        print("MODE: DRY RUN")
    print()

    try:
        conn = psycopg2.connect(
            host=args.db_host, port=args.db_port,
            user=args.db_user, password=password, database=args.db_name,
        )
    except Exception as e:
        print(f"ERROR connecting: {e}")
        return 1

    prefixes = BATCH_PREFIXES if args.batch else [args.collection_prefix]

    total_affected = 0
    total_updated = 0

    for prefix in prefixes:
        update_sql, count_sql, params = _build_sql(args.proxy_url, prefix, args.skip_thumbnails)

        cur = conn.cursor()
        cur.execute(count_sql, params)
        affected = cur.fetchone()[0]
        cur.close()

        label = f"{prefix}*" if prefix else "all collections"
        print(f"[{label}] Items needing rewrite: {affected}")
        total_affected += affected

        if affected == 0 or args.dry_run:
            continue

        try:
            cur = conn.cursor()
            print(f"[{label}] Rewriting... ", end="", flush=True)
            t0 = time.time()
            cur.execute(update_sql, params)
            rows_updated = cur.rowcount
            conn.commit()
            cur.close()
            print(f"done ({time.time() - t0:.1f}s) — {rows_updated} updated")
            total_updated += rows_updated
        except Exception as e:
            conn.rollback()
            print(f"ERROR: {e}")
            conn.close()
            return 1

    conn.close()

    if total_affected == 0:
        print("Nothing to do.")
        return 0

    if args.dry_run:
        print(f"\n[DRY RUN] No changes written to database ({total_affected} items would be rewritten)")
        return 0

    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Items updated: {total_updated}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
