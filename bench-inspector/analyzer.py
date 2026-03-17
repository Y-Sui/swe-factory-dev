"""Core analysis functions for SWE-bench instance quality inspection."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


def load_instances(path: str) -> list[dict]:
    """Load instances from JSON or JSONL file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    text = p.read_text()
    if p.suffix == ".jsonl":
        return [json.loads(line) for line in text.strip().splitlines() if line.strip()]
    else:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]


# ---------------------------------------------------------------------------
# Diff / Patch analysis
# ---------------------------------------------------------------------------

def parse_diff_stats(diff_text: str) -> dict:
    """Parse a unified diff and return stats."""
    if not diff_text:
        return {"files": 0, "additions": 0, "deletions": 0, "total_lines": 0, "files_list": []}

    files = []
    additions = 0
    deletions = 0

    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            # "diff --git a/foo b/foo" → extract "foo"
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                fname = parts[1]
                if fname not in files:
                    files.append(fname)
        elif line.startswith("+++ b/"):
            fname = line[6:]
            if fname != "/dev/null" and fname not in files:
                files.append(fname)
        elif line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1

    return {
        "files": len(set(files)),
        "additions": additions,
        "deletions": deletions,
        "total_lines": additions + deletions,
        "files_list": list(set(files)),
    }


def extract_test_names(test_patch: str) -> list[str]:
    """Extract test function/method names from a test patch."""
    if not test_patch:
        return []
    names = []
    for line in test_patch.splitlines():
        stripped = line.lstrip("+").strip()
        m = re.match(r"def (test_\w+)", stripped)
        if m:
            names.append(m.group(1))
    return names


def extract_test_files(test_patch: str) -> list[str]:
    """Extract test file paths from a test patch."""
    if not test_patch:
        return []
    files = []
    for line in test_patch.splitlines():
        if line.startswith("+++ b/"):
            fname = line[6:]
            if fname != "/dev/null":
                files.append(fname)
    return files


# ---------------------------------------------------------------------------
# Problem statement checks
# ---------------------------------------------------------------------------

@dataclass
class ProblemStatementReport:
    instance_id: str
    length: int
    word_count: int
    has_steps_to_reproduce: bool
    has_expected_behavior: bool
    has_actual_behavior: bool
    has_error_message: bool
    has_code_snippet: bool


def check_problem_statement(instance: dict) -> ProblemStatementReport:
    ps = instance.get("problem_statement", "") or ""
    ps_lower = ps.lower()

    return ProblemStatementReport(
        instance_id=instance.get("instance_id", ""),
        length=len(ps),
        word_count=len(ps.split()),
        has_steps_to_reproduce=any(kw in ps_lower for kw in [
            "steps to reproduce", "reproduce", "to replicate", "how to", "repro",
        ]),
        has_expected_behavior=any(kw in ps_lower for kw in [
            "expected", "should", "supposed to", "correct behavior",
        ]),
        has_actual_behavior=any(kw in ps_lower for kw in [
            "actual", "instead", "but got", "currently", "fails", "crash", "error",
        ]),
        has_error_message=any(kw in ps_lower for kw in [
            "traceback", "error:", "exception", "assert", "raise",
        ]) or bool(re.search(r"```", ps)),
        has_code_snippet=bool(re.search(r"```", ps)),
    )


# ---------------------------------------------------------------------------
# Gold patch checks
# ---------------------------------------------------------------------------

@dataclass
class PatchReport:
    instance_id: str
    stats: dict
    is_empty: bool
    is_test_only: bool


def check_patch(instance: dict) -> PatchReport:
    patch = instance.get("patch", "") or ""
    stats = parse_diff_stats(patch)
    is_test_only = all("test" in f.lower() for f in stats["files_list"]) if stats["files_list"] else False

    return PatchReport(
        instance_id=instance.get("instance_id", ""),
        stats=stats,
        is_empty=stats["total_lines"] == 0,
        is_test_only=is_test_only,
    )


# ---------------------------------------------------------------------------
# Test patch checks
# ---------------------------------------------------------------------------

@dataclass
class TestPatchReport:
    instance_id: str
    stats: dict
    test_names: list[str]
    test_files: list[str]
    f2p_count: int
    p2p_count: int
    has_f2p: bool
    has_p2p: bool
    has_assertions: bool
    test_quality_info: dict | None


def check_test_patch(instance: dict) -> TestPatchReport:
    test_patch = instance.get("test_patch", "") or ""
    stats = parse_diff_stats(test_patch)
    test_names = extract_test_names(test_patch)
    test_files = extract_test_files(test_patch)

    f2p_raw = instance.get("FAIL_TO_PASS", "[]")
    p2p_raw = instance.get("PASS_TO_PASS", "[]")
    f2p_list = json.loads(f2p_raw) if isinstance(f2p_raw, str) else (f2p_raw or [])
    p2p_list = json.loads(p2p_raw) if isinstance(p2p_raw, str) else (p2p_raw or [])

    has_assertions = any(
        kw in test_patch for kw in ["assert ", "assert(", "assertEqual", "assertRaises", "pytest.raises"]
    )

    return TestPatchReport(
        instance_id=instance.get("instance_id", ""),
        stats=stats,
        test_names=test_names,
        test_files=test_files,
        f2p_count=len(f2p_list),
        p2p_count=len(p2p_list),
        has_f2p=len(f2p_list) > 0,
        has_p2p=len(p2p_list) > 0,
        has_assertions=has_assertions,
        test_quality_info=instance.get("test_quality"),
    )


# ---------------------------------------------------------------------------
# Overall summary
# ---------------------------------------------------------------------------

def compute_dataset_summary(instances: list[dict]) -> dict:
    ps_reports = [check_problem_statement(i) for i in instances]
    patch_reports = [check_patch(i) for i in instances]
    test_reports = [check_test_patch(i) for i in instances]

    repos = {}
    for inst in instances:
        repo = inst.get("repo", "unknown")
        repos[repo] = repos.get(repo, 0) + 1

    return {
        "total_instances": len(instances),
        "repos": repos,
        "problem_statement": {
            "avg_length": sum(r.length for r in ps_reports) / max(len(ps_reports), 1),
            "avg_words": sum(r.word_count for r in ps_reports) / max(len(ps_reports), 1),
        },
        "patch": {
            "empty_count": sum(1 for r in patch_reports if r.is_empty),
            "avg_lines": sum(r.stats["total_lines"] for r in patch_reports) / max(len(patch_reports), 1),
            "avg_files": sum(r.stats["files"] for r in patch_reports) / max(len(patch_reports), 1),
        },
        "test_patch": {
            "no_f2p_count": sum(1 for r in test_reports if not r.has_f2p),
            "no_p2p_count": sum(1 for r in test_reports if not r.has_p2p),
            "avg_test_count": sum(len(r.test_names) for r in test_reports) / max(len(test_reports), 1),
        },
    }
