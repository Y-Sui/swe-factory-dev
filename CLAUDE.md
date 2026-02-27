# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
2. **WriteTestAgent** - Generates pytest tests when `test_patch` is missing or has < 3 files
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

Task instances (`.jsonl`) -> `app/main.py` clones repos into `testbed/` -> agents generate `Dockerfile` + `eval.sh` -> outputs saved to `--output-dir/<instance_id>/` -> successful results appended to `--results-path/results.json` -> validation via `evaluation/run_evaluation.py`

## Code Style

- Prefer minimal, targeted code changes. Avoid unnecessary refactoring or complex abstractions.
- Keep code clean and concise -- no over-engineering.
- Bash scripts must be simple, concise, and easy to read. Avoid over-abstraction like loops over config arrays or excessive variables -- prefer explicit, straightforward commands.
- Python: 4-space indentation, `snake_case` for functions/variables, `PascalCase` for classes. No enforced formatter; match nearby style.

## Configuration

- Local runs require env vars in `.env`: `OPENAI_KEY`, `OPENAI_API_BASE_URL`, optionally `GITHUB_TOKEN` (for private repos).
- `testbed/` is the default clone directory for target repos; keep it clean.
- Task timeout per subprocess: 5400s (90 min). Test execution timeout: 300s.
