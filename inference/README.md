# Inference

This folder is a two-stage workflow:
1) Build or transfer Docker images locally (produce a dataset with `docker_image`).
2) Run the editing agents against those images.

## Environment Setup

```bash
conda create -n inference python=3.13 -y
conda activate inference
pip install -r requirements-inference.txt
```

## Stage 1: Build SWE Env Images Locally

After data collection, we typically have Dockerfiles for each instance. To run
experiments, we build these images locally first. This stage also normalizes
the environment so downstream evaluation is stable and less noisy. Common
issues we handle here include:
- Reward/test execution taking too long (e.g. >300s).
- Repositories not mounted at `/testbed` (e.g., code lives under `/testbed/mypy`).
- Extra environment files under `/testbed` causing noisy diffs/patches (e.g., `/testbed/.venv`).

Use the transfer agent runner (LLM-driven) to normalize the environment and build images,
then embed artifacts (Dockerfile + eval script) into a new dataset file. Use the
dataset produced by the SWE-Builder stage (the output of `app/main.py` under the
`results` directory) as-is.

Input is simply the SWE-Builder output dataset (use it as-is).

```bash
python inference/build_image/main.py \
  --input /path/to/instances.json \
  --output /path/to/run_dir \
  --max-iterations 5 \
  --eval-timeout 300 \
  --max-workers 2
```

`--eval-timeout` defaults to 300 seconds if omitted. Tune it for your needs; instances
that exceed the timeout are marked as failures and filtered out of the transferred dataset.

Parameter reference (Stage 1):

| Parameter | Meaning | Allowed / Example |
| --- | --- | --- |
| `--input` | Path to raw instances JSON list | `/path/to/instances.json` |
| `--output` | Output directory for build artifacts | `/path/to/run_dir` |
| `--max-iterations` | LLM edit/build iterations per instance | `5` |
| `--eval-timeout` | Eval script timeout (seconds) | `300` |
| `--max-workers` | Parallel workers | `2` |
| `--skip-existing` | Skip instances with existing `summary.json` | flag |

Outputs in `--output`:
- `summary.json` / `summary_main.json`
- `<input_stem>_transferred.json` (successful entries with `docker_image`, `dockerfile`, `eval_script`).
  The `docker_image` field points to the built SWE environment image for each instance.
- `<input_stem>_failed.json`

## Stage 2: Run a Coding Agent on the Built SWE Environment

Run against the transferred dataset produced in Stage 1. We currently support
mini_swe_agent, the DeepSWE editing agent (r2egym scaffold), and OpenHands (last).
OpenHands is unofficial here and diverges significantly from the original
implementation, so please use it with caution. We aim for a closer reproduction
over time and welcome PRs. At the moment, only non-function calling mode is
supported.

The dataset should be the Stage 1 transferred output (for example,
`/path/to/run_dir/<input_stem>_transferred.json`). Each entry should include:
`instance_id`, `docker_image`, `dockerfile`, and `eval_script`.

Model calls go through LiteLLM. Set your base URL and provider API key before
running (examples below).

```bash
export LLM_BASE_URL="http://127.0.0.1:8000/v1"
export OPENAI_API_KEY="YOUR_API_KEY"
export OPENROUTER_API_KEY="YOUR_OPENROUTER_KEY"
```

DeepSWE editing agent (r2egym scaffold):
```bash
python -m inference.agenthub.run.edit runagent_multiple \
  --dataset /path/to/TRANSFERRED_DATASET.json \
  --split dev \
  --k 1 \
  --start_idx 0 \
  --max_workers 5 \
  --traj_dir ./run_logs/deepswe_run \
  --exp_name deepswe_run \
  --llm_name openai/gpt-4o-mini \
  --use_fn_calling False \
  --backend docker \
  --scaffold r2egym
```

mini_swe_agent:
```bash
python -m inference.agenthub.run.edit runagent_multiple \
  --dataset /path/to/TRANSFERRED_DATASET.json \
  --split dev \
  --k 1 \
  --start_idx 0 \
  --max_workers 5 \
  --traj_dir ./run_logs/mini_swe_run \
  --exp_name mini_swe_run \
  --llm_name openai/gpt-4o-mini \
  --use_fn_calling False \
  --backend docker \
  --scaffold mini_swe_agent
```

OpenHands (experimental):
```bash
python -m inference.agenthub.run.edit runagent_multiple \
  --dataset /path/to/TRANSFERRED_DATASET.json \
  --split dev \
  --k 1 \
  --start_idx 0 \
  --max_workers 5 \
  --traj_dir ./run_logs/openhands_run \
  --exp_name openhands_run \
  --llm_name openai/gpt-4o-mini \
  --use_fn_calling False \
  --backend docker \
  --scaffold openhands
```

Notes:
- `--split` is only a label for the local JSON loader; keep it consistent (e.g., `dev`).
- If you already have local images, you can skip Stage 1 and provide a dataset that
  includes `instance_id`, `docker_image`, `dockerfile`, and `eval_script`.
- `--backend` currently supports `docker` only.
- If running scripts from outside the repo root, set `PYTHONPATH` to the repo path.
- If you use a non-OpenAI provider, set the matching API key env var (for example, `ANTHROPIC_API_KEY`).
- This codebase is built on top of R2E-Gym; thanks to the original authors.

Parameter reference (common):

| Parameter | Meaning | Allowed / Example |
| --- | --- | --- |
| `--dataset` | Path to Stage 1 transferred dataset | `/path/to/TRANSFERRED_DATASET.json` |
| `--split` | Label for local JSON loader | `dev` |
| `--k` | Number of instances to run | `1` |
| `--start_idx` | Start index in dataset | `0` |
| `--max_workers` | Parallel workers | `5` |
| `--traj_dir` | Output directory for logs/artifacts | `./run_logs/my_run` |
| `--exp_name` | Experiment name | `my_run` |
| `--llm_name` | LiteLLM model name | `openai/gpt-4o-mini` |
| `--use_fn_calling` | Function-calling mode | `False` (only supported) |
| `--backend` | Runtime backend | `docker` |
| `--scaffold` | Agent scaffold | `r2egym` / `mini_swe_agent` / `openhands` |

## Acknowledgements

The inference/agenthub module is developed on top of R2E-Gym. Thanks to the
original authors for their work.
