#!/usr/bin/env python3
"""
Delete instances marked for deletion in annotations.jsonl.

Does two things:
  1. Removes their entries from annotations.jsonl
  2. Optionally deletes the actual instance directories from applicable_setup/

Usage:
    python scripts/delete_marked.py
    python scripts/delete_marked.py --annotations /path/to/annotations.jsonl
    python scripts/delete_marked.py --dry-run        # preview only, no changes
    python scripts/delete_marked.py --keep-dirs      # only clean annotations, don't rm dirs
"""

import argparse
import json
import shutil
import sys
from pathlib import Path


def load_annotations(path: Path) -> list[dict]:
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def write_annotations(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete instances marked for deletion in annotations.jsonl."
    )
    parser.add_argument(
        "--annotations",
        default=None,
        help="Path to annotations.jsonl (default: <project_root>/annotations.jsonl)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be deleted without making any changes.",
    )
    parser.add_argument(
        "--keep-dirs",
        action="store_true",
        help="Remove from annotations.jsonl but do NOT delete instance directories.",
    )
    args = parser.parse_args()

    if args.annotations:
        annotations_path = Path(args.annotations).resolve()
    else:
        annotations_path = Path(__file__).resolve().parent.parent / "annotations.jsonl"

    if not annotations_path.exists():
        print(f"Error: {annotations_path} not found.", file=sys.stderr)
        sys.exit(1)

    records = load_annotations(annotations_path)
    to_delete = [r for r in records if r.get("mark_for_deletion")]
    to_keep   = [r for r in records if not r.get("mark_for_deletion")]

    if not to_delete:
        print("No instances marked for deletion.")
        return

    print(f"Found {len(to_delete)} instance(s) marked for deletion:\n")
    for r in to_delete:
        instance_dir = Path(r["run_dir"]) / r["instance_id"]
        dir_exists = instance_dir.exists()
        dir_note = f"  dir: {instance_dir}" + (" [EXISTS]" if dir_exists else " [not found]")
        print(f"  • {r['instance_id']}")
        print(f"      reason={r.get('failure_reason')}  note={r.get('note') or '-'}")
        if not args.keep_dirs:
            print(dir_note)

    if args.dry_run:
        print(f"\n[dry-run] Would remove {len(to_delete)} entries from {annotations_path}")
        if not args.keep_dirs:
            existing = [
                Path(r["run_dir"]) / r["instance_id"]
                for r in to_delete
                if (Path(r["run_dir"]) / r["instance_id"]).exists()
            ]
            if existing:
                print(f"[dry-run] Would delete {len(existing)} director(y/ies)")
        return

    # Confirm
    print(f"\nThis will:")
    print(f"  • Remove {len(to_delete)} entries from {annotations_path}")
    if not args.keep_dirs:
        existing_dirs = [
            Path(r["run_dir"]) / r["instance_id"]
            for r in to_delete
            if (Path(r["run_dir"]) / r["instance_id"]).exists()
        ]
        if existing_dirs:
            print(f"  • Permanently delete {len(existing_dirs)} instance director(y/ies)")

    confirm = input("\nType 'yes' to confirm: ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        return

    # Remove annotation entries
    write_annotations(annotations_path, to_keep)
    print(f"Removed {len(to_delete)} entries from {annotations_path}")

    # Delete directories
    if not args.keep_dirs:
        deleted = 0
        for r in to_delete:
            instance_dir = Path(r["run_dir"]) / r["instance_id"]
            if instance_dir.exists():
                shutil.rmtree(instance_dir)
                print(f"  Deleted: {instance_dir}")
                deleted += 1
        if deleted:
            print(f"Deleted {deleted} director(y/ies).")
        else:
            print("No directories found to delete.")


if __name__ == "__main__":
    main()
