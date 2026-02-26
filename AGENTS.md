# Repository Guidelines

## Project Structure & Module Organization
- `app/`: core pipeline and agents (entry point `app/main.py`, agents under `app/agents/`).
- `data_collection/`: GitHub data collection and instance construction.
- `evaluation/`: Fail2Pass validation and benchmarking utilities.
- `inference/`: running coding agents on generated environments.
- `run/`: bash entrypoints for common workflows.
- `scripts/`: analysis helpers (e.g., cost, fail2pass judge).
- `testbed/`: local clones for environment setup.
- `figure/`, `README.md`, `preprint.pdf`: docs and assets.

## Build, Test, and Development Commands
- `python app/main.py swe-bench --tasks-map <instances.jsonl> --output-dir <out> --setup-dir testbed --results-path <out>/results`  
  Runs Stage II to generate Dockerfile + `eval.sh` for tasks.
- `bash run/run.sh`  
  Full pipeline (see `run/` scripts for variants).
- `python data_collection/collect/build_dataset.py <prs.jsonl> <instances.jsonl>`  
  Build task instances from PRs.
- `python data_collection/collect/get_version.py --instance_path <instances.jsonl> --testbed testbed`  
  Adds `version` fields required by Stage II.
- `python scripts/judge_fail2pass.py <eval_dir> <out.json>`  
  Fail2Pass validation results.

## Coding Style & Naming Conventions
- Python code uses 4‑space indentation; prefer explicit, readable control flow.
- Use `snake_case` for modules/functions/variables, `PascalCase` for classes.
- Keep changes minimal and localized; avoid unnecessary abstraction.
- No enforced formatter is configured; match nearby style.

## Testing Guidelines
- This repo generates **tests for target repositories**; it does not ship its own unit test suite.
- Test execution is orchestrated by generated `eval.sh` inside Docker.
- Generated tests should live under `tests/` and follow `test_*.py` naming.

## Commit & Pull Request Guidelines
- Commit history is mixed; use clear, imperative messages (optional scope), e.g.  
  `feat(inference): add --model_name for Stage 1 runner`
- PRs should include:
  - Purpose and scope summary
  - Reproduction command(s)
  - Any new outputs/paths (e.g., `run/` scripts, `results.json`)
  - Notes on cost or runtime if relevant

## Configuration Tips
- Local runs require `.env` variables (e.g., `OPENAI_KEY`, `OPENAI_API_BASE_URL`, `GITHUB_TOKEN`).
- Keep `testbed/` clean; large repo clones belong there.
