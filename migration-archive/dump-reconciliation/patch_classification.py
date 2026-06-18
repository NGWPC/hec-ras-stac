"""Historical artifact — do not run on a fresh migration.

The current build_drop_list.py includes suffix-match logic natively. This
script was a one-shot fix applied to correct outputs produced before that fix
existed. It is the direct producer of the committed drop_list.txt,
collection_overrides.tsv, and id_rewrites.tsv. Kept for audit trail only.

One-shot offline patch for the suffix-mismatch classification bug.

The dump assigns _N numeric suffixes to items whose IDs collide within a
collection. The S3 export stores those items without the suffix. When
build_drop_list.py ran, it matched on exact (id, collection, hash), so the
unsuffixed local items couldn't find their suffixed dump counterparts and were
mis-classified as B1/B2a/B2b/C/E.

This script re-classifies the affected rows using the already-emitted TSVs
(no live database needed) and regenerates all five output files.

The fix: for any local row whose (id, collection, hash) has no exact match in
the dump, check whether (hash, collection) maps to a dump entry. If so, the
dump published this item under a suffixed id — treat it as bucket A (MIGRATE)
and record the local_id → dump_id rewrite in id_rewrites.tsv.

Reads: outputs/dump_items.tsv, outputs/drop_classification.tsv
Writes: outputs/drop_classification.tsv (in-place, adds id_rewrite column),
        outputs/drop_list.txt (regenerated),
        outputs/collection_overrides.tsv (regenerated),
        outputs/id_rewrites.tsv (new)
"""
from __future__ import annotations

import collections
import csv
import sys
from pathlib import Path

OUTPUTS = Path(__file__).parent / "outputs"
CLASSIFICATION_FIELDNAMES = [
    "bucket", "decision", "id", "collection", "source_hash", "rel_path",
    "override_collection", "id_rewrite",
]


def main() -> int:
    dump_tsv = OUTPUTS / "dump_items.tsv"
    class_tsv = OUTPUTS / "drop_classification.tsv"
    drop_txt = OUTPUTS / "drop_list.txt"
    overrides_tsv = OUTPUTS / "collection_overrides.tsv"
    id_rewrites_tsv = OUTPUTS / "id_rewrites.tsv"

    for p in (dump_tsv, class_tsv):
        if not p.exists():
            sys.stderr.write(f"error: {p} not found\n")
            return 1

    # Build dump indexes
    dump_exact: dict[tuple[str, str], str] = {}   # (id, coll) → hash
    hash_coll_to_dump_id: dict[tuple[str, str], str] = {}  # (hash, coll) → dump_id
    hash_to_first_entry: dict[str, tuple[str, str]] = {}  # hash → first (dump_id, coll)
    with dump_tsv.open() as f:
        for row in csv.DictReader(f, delimiter="\t"):
            key = (row["id"], row["collection"])
            dump_exact[key] = row["source_hash"]
            hash_coll_to_dump_id[(row["source_hash"], row["collection"])] = row["id"]
            if row["source_hash"] not in hash_to_first_entry:
                hash_to_first_entry[row["source_hash"]] = (row["id"], row["collection"])

    # Re-classify rows
    updated_rows: list[dict] = []
    changed = 0
    with class_tsv.open() as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            bucket = row["bucket"]
            iid = row["id"]
            coll = row["collection"]
            src_hash = row["source_hash"]

            # Ensure id_rewrite column exists
            row.setdefault("id_rewrite", "")

            if bucket == "A":
                # For already-A rows, back-fill id_rewrite if missing and applicable.
                if not row.get("id_rewrite"):
                    dump_id = hash_coll_to_dump_id.get((src_hash, coll))
                    if dump_id is not None and dump_id != iid:
                        row = dict(row, id_rewrite=dump_id)
                        changed += 1
                updated_rows.append(row)
                continue

            # Exact match upgrade (defensive — shouldn't fire for non-A)
            if dump_exact.get((iid, coll)) == src_hash:
                row = dict(row, bucket="A", decision="MIGRATE", override_collection="", id_rewrite="")
                changed += 1
                updated_rows.append(row)
                continue

            # Catalogued: suffix-match via (hash, coll) → dump_id
            if coll:
                dump_id = hash_coll_to_dump_id.get((src_hash, coll))
                if dump_id is not None:
                    row = dict(row, bucket="A", decision="MIGRATE", override_collection="", id_rewrite=dump_id)
                    changed += 1
                    updated_rows.append(row)
                    continue

            # Uncatalogued (or catalogued with no coll hit): hash-only lookup
            entry = hash_to_first_entry.get(src_hash)
            if entry is not None:
                dump_id, dump_coll = entry
                id_rewrite = dump_id if dump_id != iid else ""
                row = dict(row, bucket="D", decision="MIGRATE", override_collection=dump_coll,
                           id_rewrite=id_rewrite)
                changed += 1
                updated_rows.append(row)
                continue

            updated_rows.append(row)

    print(f"Rows re-classified to bucket A: {changed}")

    # Write updated classification (with id_rewrite column)
    with class_tsv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CLASSIFICATION_FIELDNAMES, delimiter="\t",
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(updated_rows)
    print(f"Wrote {class_tsv}")

    # Regenerate derived output files
    drop_paths: list[str] = []
    overrides: list[tuple[str, str]] = []
    id_rewrites: list[tuple[str, str, str]] = []
    bucket_counts: collections.Counter = collections.Counter()
    decisions: collections.Counter = collections.Counter()

    for row in updated_rows:
        bucket_counts[row["bucket"]] += 1
        decisions[row["decision"]] += 1
        if row["decision"] == "DROP":
            drop_paths.append(row["rel_path"])
        if row.get("override_collection"):
            overrides.append((row["rel_path"], row["override_collection"]))
        if row.get("id_rewrite"):
            id_rewrites.append((row["rel_path"], row["id"], row["id_rewrite"]))

    with drop_txt.open("w") as f:
        f.write("# generated by build_drop_list.py (patched by patch_classification.py)\n")
        f.write("# paths are relative to the migration working dir "
                "(e.g. items/ebfe/<hash>/<id>.json)\n")
        f.write(f"# total drops: {len(drop_paths)}\n")
        for p in sorted(drop_paths):
            f.write(p + "\n")
    print(f"Wrote {drop_txt}  ({len(drop_paths)} drops)")

    with overrides_tsv.open("w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["rel_path", "override_collection"])
        for p, oc in sorted(overrides):
            w.writerow([p, oc])
    print(f"Wrote {overrides_tsv}  ({len(overrides)} overrides)")

    with id_rewrites_tsv.open("w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["rel_path", "local_id", "dump_id"])
        for p, lid, did in sorted(id_rewrites):
            w.writerow([p, lid, did])
    print(f"Wrote {id_rewrites_tsv}  ({len(id_rewrites)} id rewrites)")

    print()
    print("==> Bucket distribution after patch")
    for b in ["A", "B1", "B2a", "B2b", "C", "D", "E", "F"]:
        print(f"  {b:<4} {bucket_counts.get(b, 0):>7}")
    print(f"  {'sum':<4} {sum(bucket_counts.values()):>7}")
    print()
    print(f"  MIGRATE: {decisions.get('MIGRATE', 0) + decisions.get('KEEP', 0)}")
    print(f"  DROP:    {decisions.get('DROP', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
