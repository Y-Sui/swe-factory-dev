"""Build Docker images from a JSONL data file.

Usage: python build_docker_images.py <path_to_jsonl> [workers]

python3 run/step_2_inference/build_docker_images.py /home/yuansui/internal-swe-bench/benchmark/all_instances_annotated_20260322_v2.jsonl 20
"""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor


def image_name(instance_id: str) -> str:
    return f"swebench/sweb.eval.x86_64.{instance_id}:latest"


def build(item: dict) -> str:
    raw_id = item["instance_id"]
    iid = ("internal-swe-bench-" + raw_id.split("__", 1)[1]).lower()
    dockerfile = item.get("dockerfile", "")
    if not dockerfile:
        return f"[SKIP] {iid}: no dockerfile"

    img = image_name(iid)
    r = subprocess.run(["docker", "image", "inspect", img], capture_output=True)
    if r.returncode == 0:
        return f"[EXISTS] {iid}"

    r = subprocess.run(
        ["docker", "build", "-t", img, "-"],
        input=dockerfile.encode(),
        capture_output=True,
    )
    if r.returncode == 0:
        return f"[OK] {iid}"
    return f"[FAIL] {iid}: {r.stderr.decode()[-200:]}"


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <path_to_jsonl> [workers]")
        sys.exit(1)

    jsonl_file = sys.argv[1]
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 20

    with open(jsonl_file) as f:
        data = [json.loads(line) for line in f if line.strip()]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(build, data):
            print(result, flush=True)


if __name__ == "__main__":
    main()
