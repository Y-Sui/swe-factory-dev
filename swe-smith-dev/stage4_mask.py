"""Stage 3: Mask function bodies with LibCST and produce final JSONL.

For each ranked candidate:
1. Replace function body with docstring + raise NotImplementedError (via LibCST)
2. Generate two patches:
   - mask_patch: original → masked (applied in Docker to set up the task)
   - patch (gold): masked → original (the answer; used for verification)
3. Output SWE-bench-compatible JSONL

Evaluation flow in Docker:
  base Dockerfile clones repo at base_commit into /testbed
  → apply mask_patch → model sees masked code
  → model produces model_patch
  → apply model_patch → run tests → check pass/fail
"""

import json
import subprocess
from datetime import datetime, timezone
from difflib import unified_diff
from pathlib import Path

import libcst as cst

from config import REPOS, DOCKER_TEMPLATES, TASK_STATEMENT_TEMPLATE


# ── LibCST masker ──────────────────────────────────────────────────────

class FunctionMasker(cst.CSTTransformer):
    """Replace a specific function's body with docstring + raise NotImplementedError."""

    def __init__(self, target_func: str, target_class: str | None, generated_docstring: str = ""):
        self.target_func = target_func
        self.target_class = target_class
        self.generated_docstring = generated_docstring
        self._in_target_class = False
        self._masked = False

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        if self.target_class and node.name.value == self.target_class:
            self._in_target_class = True
        return True

    def leave_ClassDef(self, original_node, updated_node):
        if self.target_class and original_node.name.value == self.target_class:
            self._in_target_class = False
        return updated_node

    def _is_target(self, node) -> bool:
        if node.name.value != self.target_func:
            return False
        if self.target_class:
            return self._in_target_class
        return True

    def leave_FunctionDef(self, original_node, updated_node):
        if self._masked or not self._is_target(original_node):
            return updated_node

        self._masked = True

        docstring = self.generated_docstring
        if not docstring:
            docstring = _extract_docstring(original_node.body)

        stmts = []
        if docstring:
            if not (docstring.startswith('"""') or docstring.startswith("'''")):
                docstring = '"""' + docstring.replace('"""', '\\"\\"\\"') + '"""'
            stmts.append(
                cst.SimpleStatementLine(body=[
                    cst.Expr(value=cst.SimpleString(value=docstring))
                ])
            )
        stmts.append(
            cst.SimpleStatementLine(body=[
                cst.Raise(exc=cst.Call(func=cst.Name("NotImplementedError")))
            ])
        )

        return updated_node.with_changes(body=cst.IndentedBlock(body=stmts))


def _extract_docstring(body: cst.BaseSuite) -> str:
    if isinstance(body, cst.IndentedBlock):
        first = body.body[0]
        if isinstance(first, cst.SimpleStatementLine):
            if len(first.body) == 1 and isinstance(first.body[0], cst.Expr):
                val = first.body[0].value
                if isinstance(val, (cst.SimpleString, cst.ConcatenatedString, cst.FormattedString)):
                    return cst.parse_module("").code_for_node(val)
    return ""


def mask_function(file_content: str, func_name: str, class_name: str | None, generated_docstring: str = "") -> tuple[str, bool]:
    """Mask a function in the source. Returns (masked_source, success)."""
    try:
        tree = cst.parse_module(file_content)
    except cst.ParserSyntaxError:
        return file_content, False

    masker = FunctionMasker(func_name, class_name, generated_docstring)
    new_tree = tree.visit(masker)
    return new_tree.code, masker._masked


# ── Diff helpers ───────────────────────────────────────────────────────

def _get_head_commit(repo_path: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, cwd=str(repo_path),
    )
    return result.stdout.strip()


def _make_diff(a: str, b: str, file_path: str) -> str:
    """Unified diff from a → b."""
    diff = unified_diff(
        a.splitlines(keepends=True),
        b.splitlines(keepends=True),
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
    )
    return "".join(diff)


# ── Anti-leakage: find test files that reference the target function ───

# Directories that are always test-related
_TEST_DIRS = {"tests", "test", "testing", "regression_tests"}


def _find_leaking_test_files(repo_path: Path, func_name: str, class_name: str | None) -> list[str]:
    """Find test files that directly test the target function.

    Uses smarter matching to avoid over-stripping:
    - If class_name is set, search for the class name (e.g. "DPOLoss") in test files
    - If func_name is common (forward, update, etc.), require class_name match
    - Otherwise search for func_name

    Returns list of file paths relative to repo root.
    """
    # Common method names that would match too broadly
    _COMMON_NAMES = {"forward", "update", "call", "run", "process", "execute",
                     "compute", "apply", "transform", "encode", "decode", "sample"}

    # Determine search patterns
    patterns = []
    if class_name:
        patterns.append(class_name)  # e.g. "DPOLoss"
    if func_name not in _COMMON_NAMES:
        patterns.append(func_name)
    # If both are empty/common, skip stripping entirely
    if not patterns:
        return []

    leaking = []

    for test_dir_name in _TEST_DIRS:
        test_dir = repo_path / test_dir_name
        if not test_dir.is_dir():
            continue
        for py_file in test_dir.rglob("*.py"):
            rel = str(py_file.relative_to(repo_path))
            try:
                content = py_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if any(p in content for p in patterns):
                leaking.append(rel)

    for py_file in repo_path.glob("test_*.py"):
        rel = str(py_file.relative_to(repo_path))
        if rel not in leaking:
            try:
                content = py_file.read_text(encoding="utf-8", errors="ignore")
                if any(p in content for p in patterns):
                    leaking.append(rel)
            except OSError:
                pass

    return leaking


def _make_delete_patch(file_path: str, content: str) -> str:
    """Create a unified diff that deletes an entire file (content → empty)."""
    diff = unified_diff(
        content.splitlines(keepends=True),
        [],
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
    )
    return "".join(diff)


# ── Instance formatting ───────────────────────────────────────────────

def format_instance(candidate: dict, repo_key: str, index: int) -> dict:
    """Convert a ranked candidate to SWE-bench-style instance."""
    repo_config = REPOS[repo_key]
    base_commit = _get_head_commit(repo_config.path)

    repo_slug = repo_config.name
    instance_id = f"{repo_slug}-smith-{index:04d}"

    file_path = candidate["file_path"]
    original_content = (repo_config.path / file_path).read_text(encoding="utf-8", errors="ignore")

    masked_content, success = mask_function(
        original_content,
        candidate["func_name"],
        candidate.get("class_name"),
        generated_docstring=candidate.get("generated_docstring", ""),
    )

    if not success:
        print(f"    WARNING: masking failed for {candidate['func_name']}")

    # mask_patch part 1: mask the function body (original → masked)
    mask_patch = _make_diff(original_content, masked_content, file_path)

    # mask_patch part 2: delete test files that reference the target function
    leaking_files = _find_leaking_test_files(repo_config.path, candidate["func_name"], candidate.get("class_name"))
    for leak_path in leaking_files:
        leak_content = (repo_config.path / leak_path).read_text(encoding="utf-8", errors="ignore")
        mask_patch += _make_delete_patch(leak_path, leak_content)
    if leaking_files:
        print(f"    Stripping {len(leaking_files)} leaking test file(s): {', '.join(leaking_files)}")

    # gold patch: the answer, restores original (masked → original)
    gold_patch = _make_diff(masked_content, original_content, file_path)

    # test_patch: adding the generated test file (empty → test content)
    test_source = candidate.get("test_source", "")
    test_filename = f"tests/test_smith_{candidate['func_name']}_{index:04d}.py"
    test_patch = ""
    test_ids = []
    if test_source:
        test_patch = _make_diff("", test_source, test_filename)
        # Extract pytest test IDs
        for line in test_source.splitlines():
            stripped = line.strip()
            if stripped.startswith("def test_"):
                name = stripped.split("(")[0].replace("def ", "")
                module = test_filename.replace("/", ".").replace(".py", "")
                test_ids.append(f"{module}::{name}")

    repo_name = repo_config.name.replace("__", "/")
    problem_statement = TASK_STATEMENT_TEMPLATE.format(
        repo=repo_name,
        func_name=candidate["func_name"],
        file_path=file_path,
    )

    docker_template_path = DOCKER_TEMPLATES.get(repo_key)
    docker_template = ""
    if docker_template_path and docker_template_path.exists():
        docker_template = docker_template_path.read_text(encoding="utf-8")

    return {
        "instance_id": instance_id,
        "repo": repo_name,
        "base_commit": base_commit,
        "mask_patch": mask_patch,
        "patch": gold_patch,
        "test_patch": test_patch,
        "problem_statement": problem_statement,
        "hints_text": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "version": "0.1.0",
        "FAIL_TO_PASS": json.dumps(test_ids),
        "PASS_TO_PASS": "[]",
        "environment_setup_commit": base_commit,
        "docker_template": docker_template,
        "docker_image": f"internal-swe-bench-swe-smith-{repo_key}:{base_commit[:12]}",
        "stripped_test_files": leaking_files,
        "function_metadata": {
            "func_name": candidate["func_name"],
            "class_name": candidate.get("class_name"),
            "file_path": file_path,
            "lineno": candidate["lineno"],
            "end_lineno": candidate.get("end_lineno"),
            "num_lines": candidate.get("num_lines"),
            "generated_docstring": candidate.get("generated_docstring", ""),
            "grade_score": candidate.get("grade_score"),
            "grade_reason": candidate.get("grade_reason", ""),
        },
    }


def mask_and_format_all(candidates: list[dict], repo_key: str, output_path: Path) -> list[dict]:
    """Mask all candidates and write instances JSONL."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    instances = []

    for i, cand in enumerate(candidates):
        qual = f"{cand.get('class_name', '')}.{cand['func_name']}" if cand.get("class_name") else cand["func_name"]
        print(f"  [{i+1}/{len(candidates)}] Masking: {qual}")
        instance = format_instance(cand, repo_key, i)
        instances.append(instance)

    with open(output_path, "w") as f:
        for inst in instances:
            f.write(json.dumps(inst) + "\n")

    print(f"  Saved {len(instances)} instances to {output_path}")
    return instances
