"""Stage 2: AST-extract full source, signature, docstring for discovered candidates.

Takes candidate references from stage 1, extracts via AST:
- Full function source
- Signature (def line)
- Existing docstring
- Call count across the repo

Then uses Claude Agent to review docstring quality in one batch call:
- If docstring is missing or vague, generate a new one
- If docstring is good, keep it
"""

import ast
import json
import textwrap
from pathlib import Path

from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

from config import REPOS, RepoConfig, AGENT_MODEL
from stage1_discover import _parse_json_from_text


# ── AST extraction ────────────────────────────────────────────────────

def _extract_signature(node: ast.FunctionDef | ast.AsyncFunctionDef, source_lines: list[str]) -> str:
    """Extract the full function signature (def line through the colon)."""
    sig_lines = []
    for i in range(node.lineno - 1, min(node.end_lineno or node.lineno, len(source_lines))):
        line = source_lines[i]
        sig_lines.append(line)
        if line.rstrip().endswith(":"):
            break
    return "\n".join(sig_lines)


def _extract_docstring(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Extract the docstring from a function AST node."""
    if (node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)):
        return node.body[0].value.value.strip()
    return ""


def extract_function_source(repo_config: RepoConfig, candidate: dict) -> dict | None:
    """Extract full function source from repo using AST. Returns enriched candidate or None."""
    file_path = repo_config.path / candidate["file_path"]
    if not file_path.exists():
        return None

    try:
        source = file_path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return None

    source_lines = source.splitlines()
    target_name = candidate["func_name"]
    target_class = candidate.get("class_name")
    target_lineno = candidate.get("lineno", 0)

    # Build class map
    class_map: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    class_map[id(child)] = node.name

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != target_name:
            continue

        node_class = class_map.get(id(node))
        if target_class and node_class != target_class:
            continue
        if target_lineno and abs(node.lineno - target_lineno) > 5:
            continue

        start = node.lineno - 1
        end = node.end_lineno or node.lineno
        num_lines = end - start

        if num_lines < 20:
            return None

        func_source = "\n".join(source_lines[start:end])
        signature = _extract_signature(node, source_lines)
        docstring = _extract_docstring(node)

        has_type_hints = node.returns is not None or any(
            a.annotation is not None for a in node.args.args + node.args.kwonlyargs
        )

        return {
            **candidate,
            "lineno": node.lineno,
            "end_lineno": node.end_lineno,
            "num_lines": num_lines,
            "has_docstring": bool(docstring),
            "has_type_hints": has_type_hints,
            "source": func_source,
            "signature": signature,
            "docstring": docstring,
        }

    return None


# ── Call count analysis ───────────────────────────────────────────────

def count_function_calls(repo_config: RepoConfig) -> dict[str, int]:
    """Count how many times each repo-defined function is called across the codebase."""
    from collections import Counter

    py_files = []
    for src_dir in repo_config.src_dirs:
        src_path = repo_config.path / src_dir
        if src_path.is_dir():
            py_files.extend(src_path.rglob("*.py"))

    exclude = set(repo_config.exclude_dirs)
    trees = []
    for py_file in py_files:
        rel = py_file.relative_to(repo_config.path)
        if any(part in exclude for part in rel.parts):
            continue
        try:
            trees.append(ast.parse(py_file.read_text(encoding="utf-8", errors="ignore")))
        except (SyntaxError, UnicodeDecodeError):
            continue

    # Pass 1: collect defined function names
    defined: set[str] = set()
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)

    # Pass 2: count calls to defined functions only
    counts: Counter[str] = Counter()
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = None
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                if name and name in defined:
                    counts[name] += 1

    return dict(counts)


# ── Extract all + enrich ──────────────────────────────────────────────

def extract_all(repo_key: str, candidates: list[dict]) -> list[dict]:
    """Extract full source for all candidates, enrich with call counts and signatures."""
    repo_config = REPOS[repo_key]

    call_counts = count_function_calls(repo_config)
    print(f"  Scanned repo for call counts ({len(call_counts)} unique function names)")

    extracted = []
    for c in candidates:
        result = extract_function_source(repo_config, c)
        if result:
            result["call_count"] = call_counts.get(result["func_name"], 0)
            extracted.append(result)
        else:
            qual = f"{c.get('class_name', '')}.{c['func_name']}" if c.get("class_name") else c["func_name"]
            print(f"    Skipped {qual} (extraction failed or < 20 lines)")

    extracted.sort(key=lambda x: x["call_count"], reverse=True)
    return extracted


# ── LLM docstring review ─────────────────────────────────────────────

DOCSTRING_PROMPT = """Review the docstrings for these {count} Python functions from `{repo_name}`.

For each function:
- If the docstring is GOOD (clearly describes behavior, inputs, outputs, edge cases): keep it as-is
- If the docstring is MISSING or VAGUE: write a new one that describes WHAT the function does without revealing HOW (no implementation details, algorithm names, or internal variable names)

{functions_text}

Return a JSON array with one entry per function:
[{{"index": 0, "docstring": "the docstring text", "action": "kept" or "improved"}}]

Return ONLY the JSON array."""


async def review_docstrings(repo_key: str, candidates: list[dict]) -> list[dict]:
    """Review and improve docstrings for all candidates in one batch LLM call."""
    repo_config = REPOS[repo_key]

    functions_text = ""
    for i, c in enumerate(candidates):
        qual = f"{c['class_name']}.{c['func_name']}" if c.get("class_name") else c["func_name"]
        doc = c.get("docstring", "")
        doc_display = f'"""{doc}"""' if doc else "(no docstring)"
        functions_text += f"\n--- [{i}] {qual} ({c['file_path']}:{c['lineno']}, called {c.get('call_count', 0)}x) ---\n"
        functions_text += f"Signature:\n{c['signature']}\n\n"
        functions_text += f"Current docstring: {doc_display}\n\n"
        functions_text += f"Full source:\n{c['source']}\n"

    prompt = DOCSTRING_PROMPT.format(
        count=len(candidates),
        repo_name=repo_config.name,
        functions_text=functions_text,
    )

    print(f"  Reviewing docstrings for {len(candidates)} functions...")

    result_text = None
    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            allowed_tools=[],
            permission_mode="bypassPermissions",
            max_turns=3,
            model=AGENT_MODEL,
        ),
    ):
        if isinstance(message, ResultMessage):
            result_text = message.result

    if not result_text:
        print(f"  Docstring review returned no result")
        return candidates

    reviews = _parse_json_from_text(result_text)
    if not reviews:
        print(f"  Failed to parse docstring review")
        return candidates

    # Apply docstring updates
    kept = 0
    improved = 0
    for item in reviews:
        idx = item.get("index", -1)
        if not (0 <= idx < len(candidates)):
            continue
        new_doc = item.get("docstring", "")
        action = item.get("action", "kept")
        if new_doc:
            candidates[idx]["generated_docstring"] = new_doc
            if action == "improved":
                improved += 1
            else:
                kept += 1

    print(f"  Docstrings: {kept} kept, {improved} improved")
    return candidates


# ── Save/load ─────────────────────────────────────────────────────────

def save_extracted(extracted: list[dict], output_path: Path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        for c in extracted:
            f.write(json.dumps(c) + "\n")
    print(f"  Saved {len(extracted)} candidates to {output_path}")


def load_extracted(path: Path) -> list[dict]:
    if not path.exists():
        return []
    candidates = []
    with open(path) as f:
        for line in f:
            if line.strip():
                candidates.append(json.loads(line))
    return candidates
