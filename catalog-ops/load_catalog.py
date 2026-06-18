"""
Load the HEC-RAS STAC catalog into a pgSTAC database.

Auto-detects two input layouts:

  1. Local working-dir layout (from migrate.py):
        <input_dir>/
        ├── catalog.json
        ├── program_catalogs/<program>.json
        ├── collections/<collection-id>/collection.json
        └── items/<collection-id>/<item-id>/<item-id>.json

  2. Destination layout (pulled directly from S3 with
     `aws s3 sync s3://<stac-root>/hec-ras-stac/ <input_dir>/`),
     with per-program parent Catalogs:
        <input_dir>/
        ├── catalog.json
        └── <program>/                                (ble/mip/ohio/nc/mn)
            ├── catalog.json
            └── <collection-id>/
                ├── collection.json
                └── <item-id>/<item-id>.json

Detection rule: if `<input_dir>/collections/` and `<input_dir>/items/` exist,
use layout 1; otherwise use layout 2.

Loading order:
    1. Upsert every collection
    2. Walk items, group by each item's `collection` field, batch-upsert
       per group

Items carry their own `collection` field — set by rewrite_hrefs.py (real
value or "hec_ras_uncatalogued"). The path layout is purely for discovery;
the actual collection assignment comes from the JSON content.

Usage:
    # Dry run — counts only, no DB writes
    python3 load_catalog.py ~/ras-stac-migration-v2 --dry-run

    # Load into local pgSTAC (Docker)
    PGPASSWORD=devpassword python load_catalog.py ~/ras-stac-migration-v2 --db-host localhost

    # Load into EC2-hosted pgSTAC after pulling JSONs from S3 directly
    aws s3 sync s3://hv-fim-dev-stac/hec-ras-stac/ ~/load_subset/
    python3 load_catalog.py ~/load_subset --db-host localhost  # picks up /opt/hec-ras-stac/.db_password
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

DB_PASSWORD_FILE = "/opt/hec-ras-stac/.db_password"

# psycopg2 is only needed for actual loads, not --dry-run. Import lazily so the
# dry-run path works on machines without it (e.g. a laptop before deploy).
psycopg2 = None  # type: ignore


def _require_psycopg2() -> None:
    global psycopg2
    if psycopg2 is None:
        try:
            import psycopg2 as _p  # noqa: PLC0415
        except ImportError:
            print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary")
            sys.exit(1)
        psycopg2 = _p


def _connect(host: str, port: int, user: str, password: str, database: str):
    _require_psycopg2()
    return psycopg2.connect(host=host, port=port, user=user, password=password, database=database)


def _verify_pgstac(conn) -> tuple[bool, str]:
    """Verify the pgstac extension or schema is installed."""
    cur = conn.cursor()
    cur.execute("SELECT extname, extversion FROM pg_extension WHERE extname = 'pgstac';")
    row = cur.fetchone()
    if row:
        cur.close()
        return True, f"pgstac extension v{row[1]}"

    cur.execute("SELECT COUNT(*) FROM information_schema.routines "
                "WHERE routine_schema = 'pgstac' AND routine_name IN ('upsert_collection', 'upsert_items');")
    count = cur.fetchone()[0]
    cur.close()
    if count == 2:
        return True, "pgstac schema installed"
    return False, "pgstac extension/schema not found"


def _upsert_collection(conn, collection: dict[str, Any]) -> None:
    cur = conn.cursor()
    cur.execute("SELECT pgstac.upsert_collection(%s);", (json.dumps(collection),))
    conn.commit()
    cur.close()


def _upsert_items(conn, items: list[dict[str, Any]]) -> None:
    cur = conn.cursor()
    cur.execute("SELECT pgstac.upsert_items(%s);", (json.dumps(items),))
    conn.commit()
    cur.close()


def _db_stats(conn) -> dict[str, Any]:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM pgstac.collections;")
    n_collections = cur.fetchone()[0]
    cur.execute("SELECT collection, COUNT(*) FROM pgstac.items GROUP BY collection ORDER BY collection;")
    by_collection = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM pgstac.items;")
    n_items = cur.fetchone()[0]
    cur.close()
    return {"collections": n_collections, "total_items": n_items, "by_collection": by_collection}


def _detect_layout(input_dir: Path) -> str:
    """Return 'working-dir' or 'destination' based on directory shape."""
    if (input_dir / "collections").is_dir() and (input_dir / "items").is_dir():
        return "working-dir"
    return "destination"


def _find_collection_files(input_dir: Path, layout: str) -> list[Path]:
    """Return all collection.json paths under input_dir for the detected layout."""
    if layout == "working-dir":
        # <working_dir>/collections/<collection-id>/collection.json
        return sorted((input_dir / "collections").glob("*/collection.json"))
    # destination: <input_dir>/<program>/<collection-id>/collection.json
    # rglob and filter — robust to layout variations and harmless for any depth.
    return sorted(p for p in input_dir.rglob("collection.json"))


def _find_item_files(input_dir: Path, layout: str) -> list[Path]:
    """Return all item-JSON paths under input_dir for the detected layout.

    Excludes catalog.json and collection.json regardless of layout — only
    files inside per-item subdirs count.
    """
    if layout == "working-dir":
        # <working_dir>/items/<collection-id>/<item-id>/<item-id>.json
        return sorted((input_dir / "items").rglob("*.json"))
    # destination: <input_dir>/<program>/<collection-id>/<item-id>/<item-id>.json
    # Item JSONs are exactly 4 parts deep with per-program parent Catalogs.
    out: list[Path] = []
    for p in input_dir.rglob("*.json"):
        if p.name == "catalog.json" or p.name == "collection.json":
            continue
        try:
            rel = p.relative_to(input_dir)
        except ValueError:
            continue
        if len(rel.parts) == 4:
            out.append(p)
    return sorted(out)


def _load_collections(conn, input_dir: Path, layout: str, dry_run: bool) -> tuple[int, int]:
    """Upsert every collection.json. Returns (ok, failed)."""
    collection_files = _find_collection_files(input_dir, layout)
    print(f"\nCollections found: {len(collection_files)}")
    if dry_run:
        print("[DRY RUN] Would upsert each collection")
        return len(collection_files), 0

    ok = 0
    failed = 0
    for cf in collection_files:
        try:
            collection = json.loads(cf.read_text())
            _upsert_collection(conn, collection)
            ok += 1
        except Exception as e:
            if conn:
                conn.rollback()
            failed += 1
            print(f"  ERROR upserting {cf.parent.name}: {e}")
    print(f"Collections upserted: {ok}  (failed: {failed})")
    return ok, failed


def _group_items_by_collection(input_dir: Path, layout: str) -> dict[str, list[Path]]:
    """Walk item JSONs and group file paths by each item's `collection` field."""
    grouped: dict[str, list[Path]] = defaultdict(list)
    for p in _find_item_files(input_dir, layout):
        try:
            item = json.loads(p.read_text())
        except Exception as e:
            print(f"  WARNING: could not read {p}: {e}")
            continue
        col = item.get("collection")
        if not col:
            print(f"  WARNING: {p} has no `collection` field — skipping")
            continue
        grouped[col].append(p)
    return grouped


def _load_items(conn, input_dir: Path, layout: str, batch_size: int, dry_run: bool) -> tuple[int, int]:
    """Load all item JSONs, grouped by collection, batch-upserted. Returns (loaded, failed)."""
    grouped = _group_items_by_collection(input_dir, layout)
    total_files = sum(len(v) for v in grouped.values())
    print(f"\nItems found: {total_files} across {len(grouped)} collection(s)")

    if dry_run:
        for col in sorted(grouped):
            print(f"  [DRY RUN] {col}: {len(grouped[col])} items")
        return total_files, 0

    total_loaded = 0
    total_failed = 0
    for col in sorted(grouped):
        paths = grouped[col]
        items: list[dict[str, Any]] = []
        for p in paths:
            try:
                items.append(json.loads(p.read_text()))
            except Exception as e:
                print(f"  WARNING: could not read {p}: {e}")
        if not items:
            continue

        successful = 0
        failed = 0
        t0 = time.time()
        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]
            try:
                _upsert_items(conn, batch)
                successful += len(batch)
            except Exception as e:
                if conn:
                    conn.rollback()
                failed += len(batch)
                print(f"  ERROR {col} batch {i // batch_size + 1}: {e}")
        elapsed = time.time() - t0
        rate = successful / elapsed if elapsed > 0 else 0
        msg = f"  {col}: loaded {successful}/{len(items)} in {elapsed:.1f}s ({rate:.0f}/sec)"
        if failed:
            msg += f"  [{failed} failed]"
        print(msg)
        total_loaded += successful
        total_failed += failed
    return total_loaded, total_failed


def load_catalog(input_dir: Path, host: str, port: int, user: str, password: str,
                 database: str, batch_size: int, dry_run: bool) -> int:
    """Load a migration working dir or flat S3 destination tree into pgSTAC."""
    print("=" * 70)
    print("HEC-RAS STAC — pgSTAC Loader")
    print("=" * 70)

    catalog_file = input_dir / "catalog.json"
    if not catalog_file.exists():
        print(f"ERROR: catalog.json not found at {catalog_file}")
        return 1

    layout = _detect_layout(input_dir)
    print(f"\nInput dir: {input_dir}")
    print(f"Layout:    {layout}")

    if layout == "working-dir":
        # Minimum expectations for working-dir layout
        missing = [str(p) for p in (input_dir / "collections", input_dir / "items") if not p.exists()]
        if missing:
            print(f"ERROR: working-dir layout missing: {missing}")
            return 1

    catalog = json.loads(catalog_file.read_text())
    print(f"Catalog:   {catalog.get('id')} — {catalog.get('description', '')}")

    conn = None
    if not dry_run:
        print("\nConnecting to database...")
        try:
            conn = _connect(host, port, user, password, database)
        except Exception as e:
            print(f"ERROR: {e}")
            return 1
        ok, msg = _verify_pgstac(conn)
        print(f"  {msg}")
        if not ok:
            print("FATAL: pgSTAC not installed in database")
            conn.close()
            return 1
    else:
        print("\nDRY RUN — no database connection, no writes")

    _, col_failed = _load_collections(conn, input_dir, layout, dry_run)
    loaded, item_failed = _load_items(conn, input_dir, layout, batch_size, dry_run)

    if conn:
        conn.close()

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    if dry_run:
        print(f"Collections: {len(_find_collection_files(input_dir, layout))}")
        print(f"Items:       {sum(len(v) for v in _group_items_by_collection(input_dir, layout).values())}")
        print("[DRY RUN] No data written")
        return 0

    print(f"Items loaded:        {loaded}")
    print(f"Collections failed:  {col_failed}")
    print(f"Items failed:        {item_failed}")
    print()

    conn = _connect(host, port, user, password, database)
    stats = _db_stats(conn)
    conn.close()
    print("Database totals:")
    print(f"  Collections: {stats['collections']}")
    print(f"  Items:       {stats['total_items']}")
    print("  By collection:")
    for col, count in stats["by_collection"]:
        print(f"    {col}: {count}")

    return 0 if (col_failed == 0 and item_failed == 0) else 1


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
    parser = argparse.ArgumentParser(description="Load the HEC-RAS STAC migration working dir (or flat S3 destination tree) into pgSTAC")
    parser.add_argument("input_dir", type=Path,
                        help="Migration working dir (contains catalog.json + collections/ + items/), "
                             "OR a destination tree (catalog.json + <collection-id>/{collection.json, <item-id>/...}). "
                             "Layout auto-detected.")
    parser.add_argument("--db-host",     default=os.environ.get("PGHOST", "database"))
    parser.add_argument("--db-port",     type=int, default=5432)
    parser.add_argument("--db-user",     default="pgstac")
    parser.add_argument("--db-password", default=None, help=f"Or set PGPASSWORD, or place in {DB_PASSWORD_FILE}")
    parser.add_argument("--db-name",     default="stacdb")
    parser.add_argument("--batch-size",  type=int, default=100)
    parser.add_argument("--dry-run",     action="store_true")
    args = parser.parse_args()

    if not args.input_dir.exists():
        print(f"ERROR: {args.input_dir} does not exist")
        return 1

    password = None
    if not args.dry_run:
        password = _resolve_password(args)
        if password is None:
            print(f"ERROR: database password required — set --db-password, PGPASSWORD, or {DB_PASSWORD_FILE}")
            return 1

    return load_catalog(
        input_dir=args.input_dir,
        host=args.db_host,
        port=args.db_port,
        user=args.db_user,
        password=password or "",
        database=args.db_name,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
