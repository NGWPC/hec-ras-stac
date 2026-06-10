"""
Rewrite S3 asset HREFs in a live pgSTAC database to use the asset-proxy service.

Run this after load_catalog.py to make private S3 assets accessible via browser
through the asset-proxy (port 8083).

Only the `href` field on each asset is rewritten. The `s3_key` field is left
untouched — it holds the real S3 key for direct boto3 access.

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

    # Scope to a collection prefix
    sudo -E python3 rewrite_asset_urls.py --proxy-url http://$HOST_IP:8083 --collection-prefix mip
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

import psycopg2


DB_PASSWORD_FILE = "/opt/hec-ras-stac/.db_password"


def _build_sql(proxy_base: str, collection_prefix: Optional[str]) -> tuple[str, str, list]:
    proxy = proxy_base.rstrip("/")

    params: list = []
    collection_filter = ""
    if collection_prefix:
        collection_filter = "AND collection LIKE %s"
        params.append(f"{collection_prefix}%")

    update_sql = f"""
        UPDATE pgstac.items
        SET content = jsonb_set(
            content,
            '{{assets}}',
            (
                SELECT jsonb_object_agg(
                    key,
                    CASE
                        WHEN value->>'href' LIKE 's3://%%'
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

    update_sql, count_sql, params = _build_sql(args.proxy_url, args.collection_prefix)

    cur = conn.cursor()
    cur.execute(count_sql, params)
    affected = cur.fetchone()[0]
    cur.close()
    print(f"Items needing rewrite: {affected}")

    if affected == 0:
        print("Nothing to do.")
        conn.close()
        return 0

    if args.dry_run:
        print("\n[DRY RUN] No changes written to database")
        conn.close()
        return 0

    try:
        cur = conn.cursor()
        cur.execute(update_sql, params)
        rows_updated = cur.rowcount
        conn.commit()
        cur.close()
    except Exception as e:
        conn.rollback()
        print(f"ERROR: {e}")
        conn.close()
        return 1

    conn.close()

    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Items updated: {rows_updated}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
