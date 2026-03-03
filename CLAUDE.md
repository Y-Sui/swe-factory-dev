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
| `MiroMindAI/sd-torchtune` | **Private** | Python (>=3.9) | setuptools | Extensive (159 files) | Fork of pytorch/torchtune with MiroMind additions. Requires `GITHUB_TOKEN`. You can use .env token to access this folder.|

## SWE-bench Instance Data Format

Each instance in our benchmark must follow this schema (compatible with SWE-bench / SWE-bench-Pro):

```json
{
  "instance_id": "MiroMindAI__miroflow-42",
  "repo": "MiroMindAI/miroflow",
  "base_commit": "<40-char SHA before the fix PR>",
  "patch": "<unified diff of the gold fix, excluding test changes>",
  "test_patch": "<unified diff of test file changes — we use LLM to generate these test files and convert them to the diff format>",
  "problem_statement": "<GitHub issue title + body>",
  "hints_text": "<issue comments before the fix PR>",
  "created_at": "<ISO timestamp>",
  "version": "<package version string>",
  "FAIL_TO_PASS": "[\"test_id_1\", \"test_id_2\"]",
  "PASS_TO_PASS": "[\"test_id_3\"]",
  "environment_setup_commit": "<commit SHA for env setup>"
}
```

## The Core Challenge: LLM-Generated Test Cases

Since MiroThinker and miroflow have almost no tests, and sd-torchtune's existing tests may not cover specific issue-related behavior, the `WriteTestAgent` must generate high-quality test cases. This is the hardest and most important part of the pipeline.

### What Makes a Good Generated Test

The generated test files should satisfy the following requirements. The test files should have test cases for F2P and P2P. The failure should also be relevant to the bug described in the issue, not due to import errors, missing dependencies, syntax errors, or unrelated logic. The assertion error and failure message must be directly related to the issue description.

1. **Fail-to-Pass (F2P)**: The test FAILS at `base_commit` (before the gold patch) and PASSES after applying the gold patch. This is the primary signal.
2. **Pass-to-Pass (P2P)**: Regression tests that PASS both before and after the gold patch, verifying the fix doesn't break existing behavior.
3. **Failure relevance**: The test fails because of the **bug described in the issue**, not due to import errors, missing dependencies, syntax errors, or unrelated logic. The assertion error / failure message must directly relate to the issue description.
4. **Minimal fix check**: The gold patch (and ONLY the gold patch) should make the F2P test pass. Random same-file edits should NOT make the test pass. This ensures the test is specific to the actual bug.
5. **Alternative fix sensitivity**: If there are multiple valid fixes for the issue, the test should accept any correct fix — it should test the *behavior*, not the exact implementation.

### Test Quality Classification

| Result | Pre-Patch | Post-Patch | Meaning |
|--------|-----------|------------|---------|
| **FAIL2PASS** | FAIL | PASS | Desired. Test captures the bug and verifies the fix. |
| **PASS2PASS** | PASS | PASS | Test is too weak — does not capture the bug. But it is fine for regression test. You can keep it.|
| **FAIL2FAIL** | FAIL | FAIL | Environment/setup broken, or gold patch doesn't fix it. This is not what we want; fix them.|
| **PASS2FAIL** | PASS | FAIL | Test is inverted or broken — gold patch causes regression. This is also not what we want; fix them.|

### Prompting Guidance for Test Generation Agents

When generating tests, the LLM agent should be prompted to:
- Read the issue description, gold patch, and the original code where the patch will be applied (like the functions, the imports, etc.) carefully before writing any test
- Write tests that exercise the **specific behavior** described in the issue. If the issue is vague or not clear, consider the code structure.
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
4. The test runner (pytest, etc.) is available and functional (sometimes the pytest may be missing from the docker file)

To improve Dockerfile generation quality, we provide **repo-specific environment setup templates** as prompt context. The template Dockerfiles can be found in `./docker`:

- `./docker/Dockerfile.miroflow`
- `./docker/Dockerfile.mirothinker`
- `./docker/Dockerfile.sd-torchtune`

These Dockerfile templates serve as the base image. The generated Dockerfile should use `FROM` to extend this file, and add any extra dependencies required by the specific commit.

### Part 2: Test Case Validity

Generated tests must pass the F2P/P2P classification AND quality checks:

1. F2P gate: At least one test must be FAIL2PASS. If all tests are PASS2PASS, the test suite is too weak and must be regenerated.
2. Failure message relevance: The assertion error when the test fails (pre-patch) should be related to the issue description. For issues regarding feature additions, we can make this looser — the test file should fail because the pre-patch does not support the feature.
3. The test files should be generally broad to accept different implementations. We don't expect a test file that only accepts one simple solution matching the gold patch. It should also work for alternative fixes that are different from the gold patch.

For the 1, you should use the docker env to verify it. For the 2 and 3, you should call LLM to do LLM-judge.

## Implementation

We use `pip install -r requirements-inference.txt` to install the Python packages for the implementation.

Key paths:
- Bash scripts to run: `./run`
- Utility scripts: `./scripts`
- Agent workflow: `./app/agents`
- Agent prompts: `./app/prompts/prompts.py`
- LLM client: `app/model/gpt.py` (OpenAI API); register models at `app/model/register.py`

For the LLM provider, we use OpenRouter. So for each model, we have to use the format `anthropic/claude-opus-4.6`; we cannot omit the `anthropic/` prefix.

## Architecture

We use multi-agent collaboration to generate Dockerfiles, test files, and test scripts. The agents are orchestrated by `AgentsManager` (`agents_manager.py`), which runs an iterative loop up to `--conv-round-limit` rounds. Each agent has a `finish_status` flag; agents only run when their dependencies are met and they haven't finished yet.

Agents run in order each iteration:

| Step | Agent | Class | Runs when |
|------|-------|-------|-----------|
| 1 | **ContextRetrievalAgent** | `context_retrieval_agent/` | at beginning |
| 2 | **WriteDockerfileAgent** | `write_dockerfile_agent/` | after collect env info from ContextRetrievalAgent |
| 3 | **WriteTestAgent** | `write_test_agent/` | context + Dockerfile done; only if `test_patch` is empty or has < 3 files |
| 4 | **eval script generation** | inline in `AgentsManager` | context + Dockerfile + test generation done; deterministic, no LLM, no agent object |
| 5 | **TestAnalysisAgent** | `test_analysis_agent/` | all prior steps done; builds Docker, runs tests |

After step 2, WriteDockerfileAgent creates the Dockerfile. `AgentsManager` immediately runs an inner self-reflection loop (at least 2 rounds): generate Dockerfile → attempt Docker build → if build fails, feed error back to WriteDockerfileAgent and retry. Only when the build succeeds does the pipeline advance to WriteTestAgent.

After step 3, WriteTestAgent creates the test files. Step 4, `AgentsManager._generate_eval_script()` deterministically composes test files and repo config into an `eval.sh` — no LLM call. The utility functions in `write_eval_script_agent/write_eval_script_utils.py` handle post-processing (heredoc injection, sanitisation, addopts override). Step 5, TestAnalysisAgent builds the Docker image, runs the eval script pre-patch and post-patch, and produces a JSON analysis with per-agent guidance fields. `AgentsManager` routes feedback by resetting the relevant agent's `finish_status` (or `_eval_script_done` for the eval step) so it re-runs the next iteration. The feedback covers two parts: (1) Docker run results — whether the test files pass; (2) LLM-generated diagnosis — the quality of the test files.

All agents extend the `Agent` base class (`app/agents/agent.py`), which provides `MessageThread`, tool dispatch, and call tracking. Should use the messageThread to manage context for each agent. We should ensure that each agent recieves clear information from other agents. Don't use vague language during the LLM information flow.

### Model Layer (`app/model/`)

- `common.py`: `Model` ABC, model registry (`MODEL_HUB`), `LiteLLMGeneric` for arbitrary LiteLLM models
- `register.py`: Registers all built-in models (GPT, Claude, Gemini, DeepSeek, Qwen, Ollama, Groq, Azure, Bedrock)
- Use `--model <name>` for registered models or
- API config via env vars: `OPENAI_API_BASE_URL`, `OPENAI_KEY`; for private repos: `GITHUB_TOKEN`

### Key Data Flow

Task instances (`.jsonl`) -> `app/main.py` clones repos into `testbed/` -> agents generate `Dockerfile` + `eval.sh` + `test_patch` -> outputs saved to `--output-dir/<instance_id>/` -> successful results appended to `--results-path/results.json`

### Post-Fix Pipeline (`scripts/post_fix_failed_cases.py`)

A standalone repair loop that targets instances that failed to achieve FAIL2PASS. Entry point: `run/post_fix.sh`.
This bash only runs after we already finished the above agent workflow and create all the files. But still the test files are failed. Then we could call this post-fix. But remember this post-pix is just an alternative way to do. Our goal is still replied on the multi-agent workflow to generate high quality test files and prepare the enviornments.

**Input**: an `applicable_setup/` directory (from Stage II) + the original instances JSONL.
**Output**: updated `eval.sh`, `status.json`, and JSON result files written back to `applicable_setup/<instance_id>/`.

Each instance goes through up to `--max-rounds` repair rounds. Within each round there are **3 LLM roles**:

| # | Role | Prompt | Trigger |
|---|------|--------|---------|
| 1 | **Test Repair Agent** | `POST_FIX_SYSTEM_PROMPT` + `POST_FIX_USER_PROMPT` | Every round — generates new test files |
| 2 | **Eval Script Agent** | `EVAL_SCRIPT_REGEN_PROMPT` | Every round — generates `eval.sh` for the new tests |
| 3 | **Dockerfile Fix Agent** | `DOCKERFILE_FIX_SYSTEM_PROMPT` + `DOCKERFILE_FIX_USER_PROMPT` | Only when Docker build fails — fixes `Dockerfile`, reuses existing `eval.sh` |

**Round flow**:
```
Round N:
  1. Test Repair Agent  → new test files (from previous failures + test output context)
  2. Eval Script Agent  → new eval.sh
  3. Docker F2P         → build image, run tests pre-patch and post-patch
     ├─ FAIL2PASS → write back, done ✓
     ├─ FAIL2FAIL / PASS2PASS → update context, continue to Round N+1
     └─ ERROR (build failed) → Dockerfile Fix Agent → re-run Docker with same eval.sh
           ├─ FAIL2PASS → write back, done ✓
           └─ still failing → update context, continue to Round N+1
```

**Context carried across rounds** (updated each round):
- `previous_test_files` — the failing test files from the previous round
- `eval_script` — the eval.sh actually used in the previous Docker run
- `test_output_pre` / `test_output_post` — Docker output before/after gold patch
- `f2p_classification` — result of the previous round
- `dockerfile` — updated if Dockerfile Fix Agent ran successfully

## Code Style

**Guiding principle**: Code must be clean, concise, and easy to iterate on. Every change should serve the main goal — generating test files that pass F2P/P2P validation and Dockerfiles that build successfully. If it doesn't help achieve that, don't write it.

### General

- Prefer minimal, targeted code changes. Don't refactor unrelated code while fixing a bug.
- No over-engineering. If a simple `if/else` works, don't build a strategy pattern.
- Don't add speculative features, configurability, or extension points "for later". Solve the problem at hand.

### LLM Prompts

- Prefer to make the prompt concise, clear, and free of misleading information.
- Give the LLM full context instead of truncated or summarized info; if there is raw information (like error output), put it directly in the agent context.

### Python

- All code comments must be in English.
- Keep functions short and focused. If a function does "generate test + validate test + retry", split it.
- Don't duplicate utility logic. Token injection, exit code extraction, F2P classification, and Dockerfile essentials injection each belong in ONE shared place, imported everywhere.
- Error handling should be practical: catch specific exceptions, log useful context, and fail fast. Don't swallow errors silently — a hidden failure in test generation wastes an entire pipeline run.
- Avoid creating non-informative print lines;

### Bash Scripts

- Simple, concise, and easy to read. Prefer explicit, straightforward commands over loops and config arrays.
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
