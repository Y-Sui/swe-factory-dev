"""Repository configs and pipeline constants."""

from dataclasses import dataclass, field
from pathlib import Path

CACHE_DIR = Path("/home/yuansui/swe-factory-dev/testbed/_cache")
PROJECT_ROOT = Path("/home/yuansui/swe-factory-dev")


@dataclass
class RepoConfig:
    name: str  # e.g. "MiroMindAI__miroflow"
    path: Path
    python_version: str = "3.12"
    src_dirs: list[str] = field(default_factory=lambda: ["src", "."])
    exclude_dirs: list[str] = field(default_factory=lambda: [
        "test", "tests", "docs", "examples", "scripts",
        "migrations", "node_modules", "__pycache__", ".git",
        "venv", ".venv", "build", "dist", "egg-info",
    ])


@dataclass
class RepoDiscoverHints:
    """Per-repo guidance for the discovery agent."""
    description: str
    priority_dirs: list[str]
    skip_dirs: list[str]


REPOS = {
    "miroflow": RepoConfig(
        name="MiroMindAI__miroflow",
        path=CACHE_DIR / "MiroMindAI__miroflow",
        python_version="3.12",
        src_dirs=["miroflow"],
    ),
    "mirothinker": RepoConfig(
        name="MiroMindAI__MiroThinker",
        path=CACHE_DIR / "MiroMindAI__MiroThinker",
        python_version="3.12",
        src_dirs=["libs", "apps"],
    ),
    "sd-torchtune": RepoConfig(
        name="MiroMindAI__sd-torchtune",
        path=CACHE_DIR / "MiroMindAI__sd-torchtune",
        python_version="3.9",
        src_dirs=["torchtune", "miromind"],
    ),
}

DISCOVER_HINTS = {
    "miroflow": RepoDiscoverHints(
        description="Agent framework for LLM-powered tool use. Core logic is orchestration, "
                    "LLM client handling, and tool call management.",
        priority_dirs=[
            "miroflow/agents/ — orchestrator, agent loop, message handling",
            "miroflow/llm/    — LLM client implementations, response processing",
            "miroflow/tool/   — tool manager, MCP server integration",
            "miroflow/skill/  — skill system",
        ],
        skip_dirs=["miroflow/utils/", "miroflow/logging/", "miroflow/benchmark/", "config/", "scripts/", "docs/", "data/"],
    ),
    "mirothinker": RepoDiscoverHints(
        description="Deep research agent monorepo. Core logic spans agent orchestration, "
                    "answer generation, tool execution, and trace analysis.",
        priority_dirs=[
            "apps/miroflow-agent/src/core/   — orchestrator, answer generator, tool executor, context manager",
            "apps/miroflow-agent/src/llm/    — LLM client implementations",
            "apps/miroflow-agent/src/io/     — output formatting, boxed content extraction",
            "apps/visualize-trace/           — trace analysis, span processing",
            "apps/lobehub-compatibility/     — tool parser, protocol bridge",
            "libs/miroflow-tools/src/        — MCP tool implementations",
        ],
        skip_dirs=[
            "apps/miroflow-agent/src/utils/", "apps/gradio-demo/",
            "apps/collect-trace/", "apps/miroflow-agent/benchmarks/",
        ],
    ),
    "sd-torchtune": RepoDiscoverHints(
        description="LLM fine-tuning framework (fork of pytorch/torchtune). Core logic is "
                    "model components, loss functions, data processing, and training utilities.",
        priority_dirs=[
            "torchtune/modules/     — attention, position embeddings, KV cache, loss functions, MoE routers",
            "torchtune/rlhf/        — PPO, reward, advantage estimation",
            "torchtune/generation/  — text generation, sampling strategies",
            "torchtune/datasets/    — data transforms, tokenization pipelines",
            "torchtune/training/    — LR schedulers, gradient handling, pooling",
            "miromind/modules/      — MiroMind-specific losses (DPO, cross-entropy)",
            "miromind/models/       — custom model architectures",
            "miromind/sd_datasets/  — custom dataset processing",
            "miromind/protocol/     — training protocol implementations",
        ],
        skip_dirs=[
            "torchtune/utils/", "torchtune/config/", "torchtune/_cli/", "torchtune/dev/",
            "miromind/utils/", "miromind/_cli/", "miromind/tools/", "miromind/monkey/",
            "miromind/examples/", "miromind/experiments/", "recipes/", "tests/", "docs/",
        ],
    ),
}

# Claude Agent SDK model (passed to claude --model)
# AGENT_MODEL = "claude-opus-4-6"
AGENT_MODEL = "claude-sonnet-4-5"
# AGENT_MODEL = "anthropic/claude-opus-4.6"

# How many candidates to select per repo
CANDIDATES_PER_REPO = 10

# Total final samples across all repos
TOTAL_SAMPLES = 30

# Docker template paths (repo key → Dockerfile template)
DOCKER_TEMPLATES = {
    "miroflow": PROJECT_ROOT / "docker" / "Dockerfile.miroflow",
    "mirothinker": PROJECT_ROOT / "docker" / "Dockerfile.mirothinker",
    "sd-torchtune": PROJECT_ROOT / "docker" / "Dockerfile.sd-torchtune",
}

# Per-repo Docker test config: how to run pytest inside the container
DOCKER_TEST_CONFIG = {
    "miroflow": {
        "image_tag": "internal-swe-bench-miroflow:base",
        "test_dir": "/testbed",           # where to place test file
        "pytest_cmd": "uv run pytest",     # how to invoke pytest
        "python_cmd": "uv run python",
    },
    "mirothinker": {
        "image_tag": "internal-swe-bench-mirothinker:base",
        "test_dir": "/testbed/apps/miroflow-agent",
        "pytest_cmd": "uv run pytest -o 'addopts='",
        "python_cmd": "uv run python",
    },
    "sd-torchtune": {
        "image_tag": "internal-swe-bench-sd-torchtune:base",
        "test_dir": "/testbed",
        "pytest_cmd": "pytest",
        "python_cmd": "python",
    },
}

# Fixed problem statement template (no LLM needed)
TASK_STATEMENT_TEMPLATE = (
    "In the repository {repo}, the function `{func_name}` in `{file_path}` "
    "has its implementation removed and replaced with `raise NotImplementedError()`. "
    "Your task is to re-implement the function body so that all tests pass.\n\n"
    "The function signature and docstring are preserved as hints. "
    "Do NOT change the function signature or any code outside the function body."
)
