"""Annotation storage for bench-inspector: reviews, edits, and export."""

import json
from pathlib import Path
from datetime import datetime, timezone

ANNOTATIONS_DIR = Path(__file__).parent / "annotations"


def _dataset_dir(dataset_key: str) -> Path:
    d = ANNOTATIONS_DIR / dataset_key
    d.mkdir(parents=True, exist_ok=True)
    return d


def dataset_key_from_path(file_path: str) -> str:
    return Path(file_path).stem


def load_reviews(dataset_key: str) -> dict:
    p = _dataset_dir(dataset_key) / "reviews.json"
    if p.exists():
        text = p.read_text().strip()
        return json.loads(text) if text else {}
    return {}


def save_review(dataset_key: str, instance_id: str, review_data: dict):
    reviews = load_reviews(dataset_key)
    review_data["ts"] = datetime.now(timezone.utc).isoformat()
    reviews[instance_id] = review_data
    p = _dataset_dir(dataset_key) / "reviews.json"
    p.write_text(json.dumps(reviews, indent=2))


def load_edits(dataset_key: str) -> dict:
    p = _dataset_dir(dataset_key) / "edits.json"
    if p.exists():
        text = p.read_text().strip()
        return json.loads(text) if text else {}
    return {}


def save_edit(dataset_key: str, instance_id: str, edits: dict):
    all_edits = load_edits(dataset_key)
    if instance_id not in all_edits:
        all_edits[instance_id] = {}
    all_edits[instance_id].update(edits)
    all_edits[instance_id]["ts"] = datetime.now(timezone.utc).isoformat()
    p = _dataset_dir(dataset_key) / "edits.json"
    p.write_text(json.dumps(all_edits, indent=2))


def apply_edits(instances: list[dict], edits: dict) -> list[dict]:
    """Return new list with edits overlaid on originals."""
    result = []
    for inst in instances:
        iid = inst.get("instance_id", "")
        if iid in edits:
            merged = {**inst}
            for k, v in edits[iid].items():
                if k != "ts":
                    merged[k] = v
            result.append(merged)
        else:
            result.append(inst)
    return result


def export_merged(instances: list[dict], reviews: dict, output_path: str):
    """Export instances (already with edits applied) plus review info."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        for inst in instances:
            out = {**inst}
            iid = inst.get("instance_id", "")
            if iid in reviews:
                out["review"] = reviews[iid]
            f.write(json.dumps(out) + "\n")
