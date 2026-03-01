# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Goal

Build an **internal SWE-bench** benchmark from three MiroMind GitHub repositories. Unlike the original SWE-bench (which targets repos with extensive existing test suites), our target repos have **very limited or no test coverage**, so we must generate test cases ourselves using LLM-powered agents.

We follow the data structures of [SWE-bench](https://github.com/SWE-bench/SWE-bench) and [SWE-bench-Pro](https://github.com/scaleapi/SWE-bench_Pro-os) to produce compatible evaluation instances.

## Target Repositories

| Repo | Visibility | Language | Build System | Existing Tests | Notes |
|------|-----------|----------|-------------|----------------|-------|
| `MiroMindAI/MiroThinker` | Public | Python (>=3.12) | uv + hatchling | Minimal (2 files) | Monorepo: `apps/`, `libs/`. Deep research agent. 6.4k stars. |
| `MiroMindAI/miroflow` | Public | Python (>=3.12) | uv + hatchling | **None** | Agent framework. `src/` layout. 2.6k stars. |
| `MiroMindAI/sd-torchtune` | **Private** | Python (>=3.9) | setuptools | Extensive (159 files) | Fork of pytorch/torchtune with MiroMind additions. Requires `GITHUB_TOKEN`. |

Key repo-specific details:
- **MiroThinker**: Uses `justfile` for tasks, deps include `anthropic`, `openai`, `mcp`, `fastmcp`, `e2b-code-interpreter`, `hydra-core`. CI: ruff lint only.
- **miroflow**: Deps include `anthropic`, `openai`, `mcp`, `fastmcp`, `e2b-code-interpreter`, `hydra-core`, `rich`. CI: ruff lint + PR title check. Entry point: `main.py`.
- **sd-torchtune**: Deps include `torch`, `torchdata`, `liger-kernel`, `datasets`, `huggingface_hub`, `sentencepiece`, `tiktoken`. CLI entry: `tune` command. Full CI: unit, e2e, GPU, daily, regression, RL tests, lint, docs, wheels, export. Install: `pip install -e ".[dev]"`.

## SWE-bench Instance Data Format

Each instance in our benchmark must follow this schema (compatible with SWE-bench / SWE-bench-Pro):

```json
{
  "instance_id": "MiroMindAI__miroflow-42",
  "repo": "MiroMindAI/miroflow",
  "base_commit": "<40-char SHA before the fix PR>",
  "patch": "<unified diff of the gold fix, excluding test changes>",
  "test_patch": "<unified diff of test file changes — LLM-generated for our repos>",
  "problem_statement": "<GitHub issue title + body>",
  "hints_text": "<issue comments before the fix PR>",
  "created_at": "<ISO timestamp>",
  "version": "<package version string>",
  "FAIL_TO_PASS": "[\"test_id_1\", \"test_id_2\"]",
  "PASS_TO_PASS": "[\"test_id_3\"]",
  "environment_setup_commit": "<commit SHA for env setup>"
}
```

SWE-bench-Pro extends this with: `requirements`, `interface`, `repo_language`, `issue_specificity`, `issue_categories`, `before_repo_set_cmd`, `selected_test_files_to_run`, `dockerhub_tag`. Include these where applicable.

## The Core Challenge: LLM-Generated Test Cases

Since MiroThinker and miroflow have almost no tests, and sd-torchtune's existing tests may not cover specific issue-related behavior, the `WriteTestAgent` must generate high-quality test cases. This is the hardest and most important part of the pipeline.

### What Makes a Good Generated Test

A generated test must satisfy ALL of:

1. **Fail-to-Pass (F2P)**: The test FAILS at `base_commit` (before the gold patch) and PASSES after applying the gold patch. This is the primary signal.
2. **Pass-to-Pass (P2P)**: Regression tests that PASS both before and after the gold patch, verifying the fix doesn't break existing behavior.
3. **Failure relevance**: The test fails because of the **bug described in the issue**, not due to import errors, missing dependencies, syntax errors, or unrelated logic. The assertion error / failure message must directly relate to the issue description.
4. **Minimal fix check**: The gold patch (and ONLY the gold patch) should make the F2P test pass. Random same-file edits should NOT make the test pass. This ensures the test is specific to the actual bug.
5. **Alternative fix sensitivity**: If there are multiple valid fixes for the issue, the test should accept any correct fix — it should test the *behavior*, not the exact implementation.

### Test Quality Classification

| Result | Pre-Patch | Post-Patch | Meaning |
|--------|-----------|------------|---------|
| **FAIL2PASS** | FAIL | PASS | Desired. Test captures the bug and verifies the fix. |
| **PASS2PASS** | PASS | PASS | Test is too weak — does not capture the bug. |
| **FAIL2FAIL** | FAIL | FAIL | Environment/setup broken, or gold patch doesn't fix it. |
| **PASS2FAIL** | PASS | FAIL | Test is inverted or broken — gold patch causes regression. |

### Prompting Guidance for Test Generation Agents

When generating tests, the LLM agent should be prompted to:
- Read the issue description and gold patch carefully before writing any test
- Write tests that exercise the **specific behavior** described in the issue, not the code structure
- Use assertions that produce meaningful failure messages tied to the bug
- Avoid overfitting to the exact patch diff — test the observable behavior change
- Include both F2P tests (targeting the bug) and P2P tests (regression guards)
- For Python repos: prefer `pytest`-style tests with descriptive names like `test_<issue_behavior>_<expected_outcome>`

## Evaluation Criteria

### Part 1: Dockerfile Validity

The generated Dockerfile must build successfully. To verify:

1. `docker build` completes without errors
2. The container starts and the repo is cloned at `/testbed` at the correct `base_commit`
3. All project dependencies are installed and importable
4. The test runner (pytest, etc.) is available and functional

To improve Dockerfile generation quality, provide **repo-specific environment setup templates** as prompt context. These templates should be derived from each repo's actual build system:

- **MiroThinker/miroflow**: Python 3.12+, `uv` package manager, `hatchling` build backend
- **sd-torchtune**: Python 3.9+, `setuptools`, `pip install -e ".[dev]"`, may need PyTorch/CUDA base image

### Part 2: Test Case Validity

Generated tests must pass the F2P/P2P classification AND quality checks:

1. **F2P gate**: At least one test must be FAIL2PASS. If all tests are PASS2PASS, the test suite is too weak and must be regenerated.
2. **Failure message relevance**: The assertion error when the test fails (pre-patch) must relate to the issue description. Check: does the error message mention the same concepts/values as the issue?
3. **Minimal fix check**: Apply the gold patch — test passes. Apply random same-file edits instead — test should still fail. This can be automated by generating N random edits to the same files as the gold patch and verifying the test still fails.
4. **Alternative fix sensitivity**: The test should verify correct behavior, not exact implementation. If the issue says "function returns wrong value", test the return value, don't assert on internal variable names.

### Exit Code Capture

Test results are captured via the `OMNIGRIL_EXIT_CODE` marker in eval.sh output:
```bash
pytest tests/test_foo.py; rc=$?; echo "OMNIGRIL_EXIT_CODE=$rc"
```
The regex `r"OMNIGRIL_EXIT_CODE=(\d+)"` extracts the exit code. 0 = pass, non-zero = fail.

## What Is SWE-Factory

An automated pipeline for GitHub issue resolution data collection and evaluation benchmark construction. It collects raw issue data, uses an LLM-powered multi-agent system (SWE-Builder) to generate Docker-based evaluation environments (Dockerfile + eval.sh), validates them via Fail2Pass testing, and supports running coding agents against the generated environments.

## Environment Setup

```bash
conda create --name swe-factory python=3.12.5 -y
conda activate swe-factory
pip install -r requirements.txt
```

For inference only: `pip install -r requirements-inference.txt` (separate conda env with Python 3.13 recommended).

Requires Docker (tested with v27.0.3-1) and Ubuntu 22.04.

## Common Commands

### Stage II: Generate evaluation environments (Dockerfile + eval.sh)

```bash
export OPENAI_API_BASE_URL=<your_base_url>
export OPENAI_KEY=<your_key>

python app/main.py swe-bench \
    --model gpt-4.1-mini \
    --tasks-map <instances.jsonl> \
    --num-processes 10 \
    --model-temperature 0.2 \
    --conv-round-limit 10 \
    --output-dir <output_dir> \
    --setup-dir testbed \
    --results-path <output_dir>/results
```

Run a single task: add `--task <instance_id>`.
Run a batch from a file: add `--task-list-file <file_with_ids>`.

### Full pipeline (batched)

```bash
bash run/run.sh
```

### Stage III: Fail2Pass validation

```bash
# Generate test logs (before/after gold patch)
python evaluation/run_evaluation.py \
  --dataset_name <results.json> \
  --predictions_path gold \
  --max_workers 5 \
  --run_id <run_id> \
  --output_path run_instances \
  --timeout 3600 \
  --is_judge_fail2pass

# Judge results
python scripts/judge_fail2pass.py <eval_dir> <output.json>
```

### Test F2P on existing artifacts (no LLM)

```bash
bash run/test_f2p.sh [optional_output_dir]
```

### Inference: run coding agents on generated environments

```bash
# Stage 1: build/normalize images
python inference/build_image/main.py --input <instances.json> --output <run_dir> --model_name <model>

# Stage 2: run agent
python -m inference.agenthub.run.edit runagent_multiple \
  --dataset <transferred.json> --scaffold mini_swe_agent --llm_name openai/gpt-4o-mini ...
```

## Architecture

### Three-Stage Pipeline

1. **Stage I (Data Collection)** - `data_collection/`: Collects raw issue data from GitHub using APIs. `collect/` gathers PRs, `versioning/` adds version metadata.
2. **Stage II (Environment Setup)** - `app/`: SWE-Builder multi-agent system generates Dockerfiles and eval scripts. Entry point: `app/main.py`.
3. **Stage III (Validation)** - `evaluation/` + `scripts/`: Runs Fail2Pass validation in Docker. `inference/`: Runs coding agents against built environments.

### SWE-Builder Agent System (`app/agents/`)

Orchestrated by `AgentsManager` (`agents_manager.py`), which runs an iterative loop (up to `--conv-round-limit` rounds):

1. **ContextRetrievalAgent** - Gathers repo setup info, READMEs, test commands
2. **WriteTestAgent** - Generates pytest tests when `test_patch` is missing or has < 3 files. **Critical for our project** since target repos lack tests.
3. **WriteDockerfileAgent** - Generates Dockerfile for the evaluation environment
4. **WriteEvalScriptAgent** - Generates `eval.sh` to run tests inside the container
5. **TestAnalysisAgent** - Validates environment by building Docker + running tests, classifies result as FAIL2PASS/PASS2PASS/FAIL2FAIL/PASS2FAIL, routes feedback to other agents

All agents extend `Agent` base class (`app/agents/agent.py`) which provides `MessageThread`, tool dispatch, and call tracking. The `AgentsManager` uses a **Memory Pool** (`results.json`) to reuse successful setups for the same repo.

### Model Layer (`app/model/`)

- `common.py`: `Model` ABC, model registry (`MODEL_HUB`), `LiteLLMGeneric` for arbitrary LiteLLM models
- `register.py`: Registers all built-in models (GPT, Claude, Gemini, DeepSeek, Qwen, Ollama, Groq, Azure, Bedrock)
- Use `--model <name>` for registered models or `--model litellm-generic-<provider/model>` for any LiteLLM-supported model
- API config via env vars: `OPENAI_API_BASE_URL`, `OPENAI_KEY`; for private repos: `GITHUB_TOKEN`

### Inference Module (`inference/`)

Built on R2E-Gym. Two stages: image building (`inference/build_image/`) and agent execution (`inference/agenthub/`). Supports scaffolds: `mini_swe_agent`, `live_swe_agent`, `r2egym` (DeepSWE), `openhands`.

### Key Data Flow

Task instances (`.jsonl`) -> `app/main.py` clones repos into `testbed/` -> agents generate `Dockerfile` + `eval.sh` + `test_patch` -> outputs saved to `--output-dir/<instance_id>/` -> successful results appended to `--results-path/results.json` -> validation via `evaluation/run_evaluation.py`

## Code Style

**Guiding principle**: Code must be clean, concise, and easy to iterate on. Every change should serve the main goal — generating test files that pass F2P/P2P validation and Dockerfiles that build successfully. If it doesn't help achieve that, don't write it.

### General

- Prefer minimal, targeted code changes. Don't refactor unrelated code while fixing a bug.
- No over-engineering. If a simple `if/else` works, don't build a strategy pattern.
- Don't add speculative features, configurability, or extension points "for later". Solve the problem at hand.
- When iterating on a failing pipeline (e.g. Dockerfile won't build, test is FAIL2FAIL), make one change at a time so you can isolate what fixed it.

### Python

- 4-space indentation, `snake_case` for functions/variables, `PascalCase` for classes. No enforced formatter; match nearby style.
- All code comments must be in English.
- Keep functions short and focused. If a function does "generate test + validate test + retry", split it.
- Avoid deep nesting — early returns are preferred over nested `if/else` blocks.
- Don't duplicate utility logic. Token injection, exit code extraction, F2P classification, and Dockerfile essentials injection each belong in ONE shared place, imported everywhere.
- Error handling should be practical: catch specific exceptions, log useful context, and fail fast. Don't swallow errors silently — a hidden failure in test generation wastes an entire pipeline run.

### Bash Scripts

- Simple, concise, and easy to read. Prefer explicit, straightforward commands over loops and config arrays.
- Always use `set -euo pipefail` at the top of scripts.
- When running parallel background processes, collect and check exit codes — don't let failures go unnoticed.

### Dockerfiles

- Keep them minimal: base image, install deps, clone repo, done. Every extra layer is a potential build failure.
- Never embed secrets (GITHUB_TOKEN) directly in Dockerfile content. Use `--build-arg` or BuildKit `--secret`.
- Always inject essentials (`curl`, `git`, `ca-certificates`) right after `FROM`.
- Test the Dockerfile builds before moving on to test generation — a broken image wastes all downstream work.

### Test Generation (LLM-Generated Code)

- Generated tests are the most critical output. Prioritize test correctness over code elegance.
- A test that achieves FAIL2PASS with simple assertions is better than an elaborate test suite that is PASS2PASS.
- When a generated test fails validation (FAIL2FAIL, PASS2PASS), iterate: read the failure output, diagnose the root cause, and adjust the prompt or test — don't blindly retry with the same approach.

## Configuration

- Local runs require env vars in `.env`: `OPENAI_KEY`, `OPENAI_API_BASE_URL`, optionally `GITHUB_TOKEN` (for private repos).
- `GITHUB_TOKEN` with `repo` scope is **required** for `MiroMindAI/sd-torchtune` (private repo) — cloning, API calls, and Docker builds all need it.
- `testbed/` is the default clone directory for target repos; keep it clean.
- Task timeout per subprocess: 5400s (90 min). Test execution timeout: 300s.
