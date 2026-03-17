"""Run coverage + pytest inside Docker for a single SWE-bench instance."""

import json
import re
import tempfile
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


# Map repo name to existing swe-smith base images
_BASE_IMAGES = {
    "MiroMindAI/miroflow": "internal-swe-bench-miroflow:base",
    "MiroMindAI/MiroThinker": "internal-swe-bench-mirothinker:base",
    "MiroMindAI/sd-torchtune": "internal-swe-bench-sd-torchtune:base",
}


def image_name_for(instance: dict) -> str:
    # swe-smith instances: use the shared base image directly
    if "-smith-" in instance.get("instance_id", ""):
        repo = instance.get("repo", "")
        if repo in _BASE_IMAGES:
            return _BASE_IMAGES[repo]
    # internal-bench: reuse the image built by the main pipeline
    raw_id = instance.get("instance_id", "unknown")
    iid = "internal-swe-bench-" + raw_id.split("__", 1)[1] if "__" in raw_id else raw_id
    return f"swebench/sweb.eval.x86_64.{iid}:latest"


def image_exists(instance: dict) -> bool:
    name = image_name_for(instance)
    r = subprocess.run(["docker", "image", "inspect", name], capture_output=True)
    return r.returncode == 0


def build_image(instance: dict) -> tuple[bool, str]:
    """Ensure the Docker image exists, rebuilding from dockerfile if needed."""
    if image_exists(instance):
        return True, "OK"

    # swe-smith uses docker_template, internal-bench uses dockerfile
    dockerfile_content = instance.get("dockerfile") or instance.get("docker_template", "")
    if not dockerfile_content:
        return False, "No dockerfile found"

    name = image_name_for(instance)
    with tempfile.TemporaryDirectory() as tmpdir:
        Path(tmpdir, "Dockerfile").write_text(dockerfile_content)
        cmd = ["docker", "build", "-t", name, tmpdir]
        token = _get_github_token()
        if token:
            cmd = ["docker", "build", "--build-arg", f"GITHUB_TOKEN={token}", "-t", name, tmpdir]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        return False, r.stderr[-2000:]
    return True, "OK"


def build_all_images(instances: list[dict], progress_callback=None, max_workers: int = 8) -> dict[str, str]:
    """Build images for all instances in parallel. Returns {instance_id: status}."""
    results = {}
    lock = threading.Lock()
    done_count = [0]
    total = len(instances)

    def _build_one(inst):
        iid = inst.get("instance_id", "unknown")
        if image_exists(inst):
            status = "cached"
        else:
            ok, log = build_image(inst)
            status = "ok" if ok else f"failed: {log[:200]}"
        with lock:
            results[iid] = status
            done_count[0] += 1
            if progress_callback:
                progress_callback(done_count[0], total, iid, status)
        return iid, status

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(pool.map(_build_one, instances))

    return results


# ---------------------------------------------------------------------------
# Coverage script generation
# ---------------------------------------------------------------------------

def _get_files_from_diff(diff_text: str) -> list[str]:
    """Extract file paths from a diff (supports both diff --git and --- a/+++ b/ formats)."""
    files = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            parts = line.split(" b/", 1)
            if len(parts) == 2 and parts[1] not in files:
                files.append(parts[1])
        elif line.startswith("+++ b/"):
            f = line[6:]
            if f != "/dev/null" and f not in files:
                files.append(f)
    return files


def _get_patch_source_files(patch: str) -> list[str]:
    return [f for f in _get_files_from_diff(patch) if not f.startswith("tests/") and not f.startswith("test_")]


def _extract_test_files_from_patch(test_patch: str) -> list[str]:
    return _get_files_from_diff(test_patch)


def _extract_test_content(test_patch: str) -> dict[str, str]:
    files = {}
    current_file = None
    lines = []

    for line in test_patch.splitlines():
        if line.startswith("+++ b/"):
            if current_file and lines:
                files[current_file] = "\n".join(lines)
            current_file = line[6:]
            lines = []
        elif line.startswith("@@") or line.startswith("--- "):
            continue
        elif line.startswith("diff --git"):
            continue
        elif current_file:
            if line.startswith("+"):
                lines.append(line[1:])
            elif not line.startswith("-"):
                lines.append(line)

    if current_file and lines:
        files[current_file] = "\n".join(lines)
    return files


def build_coverage_script(instance: dict) -> str:
    patch = instance.get("patch", "")
    test_patch = instance.get("test_patch", "")
    repo = instance.get("repo", "")

    test_files = _extract_test_files_from_patch(test_patch)
    test_contents = _extract_test_content(test_patch)

    workdir = "/testbed"
    coverage_cmd = "coverage"
    if "miroflow" in repo:
        coverage_cmd = ".venv/bin/coverage"
    elif "MiroThinker" in repo:
        workdir = "/testbed/apps/miroflow-agent"
        coverage_cmd = ".venv/bin/coverage"

    base_commit = instance.get("base_commit", "")

    script_lines = [
        "#!/bin/bash",
        "set -uxo pipefail",
        "cd /testbed",
    ]

    if base_commit:
        script_lines.append(f"git checkout {base_commit} 2>/dev/null || true")

    script_lines += [f"cd {workdir}", ""]

    for fpath, content in test_contents.items():
        dir_part = str(Path(fpath).parent)
        script_lines.append(f'mkdir -p "{dir_part}"')
        script_lines.append(f"cat <<'EOF_COV_TEST' > \"{fpath}\"")
        script_lines.append(content)
        script_lines.append("EOF_COV_TEST")
        script_lines.append("")

    # For swe-smith: base_commit already has the full code, no need to apply patch.
    # For internal-bench: need to apply gold patch on top of base_commit.
    is_swe_smith = "-smith-" in instance.get("instance_id", "")
    if not is_swe_smith:
        script_lines += [
            "cat <<'EOF_GOLD_PATCH' > /tmp/gold.diff",
            patch,
            "EOF_GOLD_PATCH",
            "cd /testbed",
            "git apply -p1 /tmp/gold.diff 2>/dev/null || patch --batch --fuzz=5 -p1 -i /tmp/gold.diff 2>/dev/null || true",
            f"cd {workdir}",
            "",
        ]

    test_file_args = " ".join(f'"{f}"' for f in test_files) if test_files else "tests/"
    script_lines += [
        f'{coverage_cmd} run --branch -m pytest {test_file_args} -v --override-ini="addopts=" 2>&1',
        "rc=$?",
        f"{coverage_cmd} json -o /tmp/cov.json 2>/dev/null || true",
        'echo "===COVERAGE_JSON_START==="',
        "cat /tmp/cov.json 2>/dev/null || echo '{}'",
        'echo "===COVERAGE_JSON_END==="',
        "exit $rc",
    ]

    return "\n".join(script_lines)


# ---------------------------------------------------------------------------
# Run coverage (image must already exist)
# ---------------------------------------------------------------------------

def run_coverage_in_docker(instance: dict, timeout: int = 300) -> dict:
    name = image_name_for(instance)

    if not image_exists(instance):
        ok, log = build_image(instance)
        if not ok:
            return {"error": f"Docker build failed:\n{log}"}

    cov_script = build_coverage_script(instance)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
        f.write(cov_script)
        script_path = f.name

    try:
        run_cmd = [
            "docker", "run", "--rm",
            "-v", f"{script_path}:/run_cov.sh:ro",
            "--platform", "linux/x86_64",
            name,
            "/bin/bash", "/run_cov.sh",
        ]
        result = subprocess.run(run_cmd, capture_output=True, text=True, timeout=timeout)
    finally:
        Path(script_path).unlink(missing_ok=True)

    output = result.stdout + result.stderr
    cov_data = _extract_coverage_json(output)

    return {
        "exit_code": result.returncode,
        "output": output[-5000:],
        "coverage": cov_data,
    }


def _extract_coverage_json(output: str) -> dict:
    m = re.search(r"===COVERAGE_JSON_START===\s*(.*?)\s*===COVERAGE_JSON_END===", output, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return {}


def format_coverage_summary(cov_data: dict, patch: str = "") -> dict:
    """Format coverage JSON into a summary, filtered to files touched by the gold patch."""
    if not cov_data:
        return {"total_coverage": None, "files": []}

    files_data = cov_data.get("files", {})

    # Filter to only files mentioned in the gold patch
    patch_files = set(_get_patch_source_files(patch)) if patch else set()

    file_summaries = []
    total_stmts = 0
    total_miss = 0
    total_branches = 0
    total_branch_miss = 0

    for fname, fdata in files_data.items():
        # Match if the coverage file path ends with any patch file path
        if patch_files and not any(fname.endswith(pf) for pf in patch_files):
            continue
        s = fdata.get("summary", {})
        stmts = s.get("num_statements", 0)
        miss = s.get("missing_lines", 0)
        branches = s.get("num_branches", 0)
        branch_miss = s.get("missing_branches", 0)
        file_summaries.append({
            "file": fname,
            "stmts": stmts,
            "miss": miss,
            "branches": branches,
            "branch_miss": branch_miss,
            "cover%": round(s.get("percent_covered", 0), 1),
        })
        total_stmts += stmts
        total_miss += miss
        total_branches += branches
        total_branch_miss += branch_miss

    total_covered = (total_stmts - total_miss) + (total_branches - total_branch_miss)
    total_total = total_stmts + total_branches
    total_pct = (total_covered / total_total * 100) if total_total > 0 else None

    return {
        "total_coverage": total_pct,
        "total_stmts": total_stmts,
        "total_miss": total_miss,
        "total_branches": total_branches,
        "total_branch_miss": total_branch_miss,
        "files": sorted(file_summaries, key=lambda x: x["cover%"]),
    }


def _get_github_token() -> str:
    env_path = Path("/home/yuansui/swe-factory-dev/.env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("GITHUB_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""
