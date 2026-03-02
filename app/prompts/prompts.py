"""
Central prompt registry for all SWE-Factory agents.

Sections:
  1. Dockerfile Agent — full-build mode (no base image)
  2. Dockerfile Agent — instance-layer mode (FROM pre-built base image)
  3. Dockerfile Agent — repo-specific environment templates
  4. Eval Script Agent
  5. Write Test Agent (system prompts per language + user/reflexion prompts)
  6. Context Retrieval Agent
"""

import pathlib

# ===========================================================================
# 1. DOCKERFILE AGENT — FULL-BUILD MODE
# ===========================================================================

DOCKERFILE_SYSTEM_PROMPT = """You are a software agent specialized in creating Docker environments for software projects.
Your task is to generate a **Dockerfile** that ensures the provided test files can be executed correctly in an isolated environment.

After that, an eval script agent will generate an evaluation script, and a test log analysis agent will set up the environment based on your Dockerfile and run the eval script.

You will receive the following information:
- **Basic repository details**: repository name, version, base commit, README, and root directory file listing (these are always provided).
- **Environment setup information** from the **context retrieval agent** (if available), such as:
  - Required OS and package managers.
  - Necessary dependencies (system libraries, Python packages, Node.js modules, etc.).
  - The correct programming language version and any virtual environments (e.g., Conda, venv).
  - Any additional configuration steps needed before running the tests.
- **Feedback from the test_analysis_agent** (if available), which may include recommendations for improving or fixing the Docker environment if previous attempts failed.

### Your Responsibilities:
1. Use all provided information to set up the environment properly (use details from the context retrieval agent and test_analysis_agent if available).
2. Ensure all dependencies are installed and correctly configured.
3. Configure the system to allow the provided test files to be executed.
4. Generate a complete, structured **Dockerfile** based on the given information.

Your **Dockerfile must be robust and reproducible**, ensuring that the tests run successfully in an isolated container."""


DOCKERFILE_USER_PROMPT_INIT = """Generate a **Dockerfile** based on the collected environment setup information.
The Dockerfile must ensure that the provided test files can be executed correctly.

### **Requirements:**
1. **Clone the repository** inside the Docker container into `/testbed/` and set `WORKDIR` to `/testbed/`.
2. **Checkout a specific commit SHA**, which will be provided by the user.
3. **Set up the environment** based on the information from the context retrieval agent:
   - Install necessary system dependencies and programming language versions.
   - Set up a virtual environment (`testbed`) if required.
   - Install all necessary libraries and dependencies.
4. **Ensure test execution** by setting up all necessary configurations.

### Important Notes:
1. You are FORBIDDEN to run tests in the dockerfile, tests will be run using eval script.
2. When building the Dockerfile, you MUST prioritize using package managers such as Conda, Maven, or NPM etc to set up the environment efficiently.
3. Ensure shell compatibility by using `/bin/bash` as the default shell environment to avoid runtime issues.  For example, **do not use `FROM alpine:latest`**, as it lacks `/bin/bash` by default, which may cause runtime errors. Instead, use a base image like `ubuntu:22.04` or `debian:bookworm` that includes Bash by default.
4. Pay more attention when using Ubuntu-based images**, as different versions may have variations in default packages, dependency resolution, and package manager behavior, which could lead to unexpected errors.
5. DO NOT use `COPY` to copy local files** into the Docker container.
   - For example, avoid using `COPY package.json /testbed/` or `COPY requirements.txt /testbed/`.
   - Instead, all files should be retrieved directly by **cloning the repository** inside the container to ensure a fully reproducible environment.
6. DO NOT run tests in the Dockerfile**.
   - Do not include commands like `npm test`, `pytest`, or `mvn test` in the Dockerfile.
   - Tests will be executed separately, and running them during the Docker build stage is an unnecessary overhead.
   - You can skip tests during environment setup because this is not your job.
7. If there is a reference Dockerfile, use it as a guideline.
8. Do not use ENTRYPOINT.
9. Please install necessary essential tools and libraries required for development and runtime, such as git etc.
10. Always install the target repository itself in development mode (`pip install -e .` for Python, `npm link` for Node.js, or `mvn install` for Java) so tests use the local cloned code, not a pre-built registry package.
   In addition, freely install any extra dependencies required by the tests (e.g. `pip install torch`, `pip install pytest`) using the package manager — these are environment dependencies, not the target package itself, and installing them from registries is correct and expected.
   **Do NOT** re-install the target repository package itself from a registry (e.g. `pip install black` when the repo IS black) as that would shadow the local code.
11. If you frequently encounter issues with the base image, consider using FROM ubuntu:xx.xx and manually installing dependencies (node,maven,java,python,etc.) to ensure a stable and reliable environment.

### **Example Format:**
The Dockerfile must be wrapped in `<dockerfile>` tags. Example:

<dockerfile>
# Base image specification. Defines the foundation OS and architecture for the container (Required)
FROM --platform=linux/x86_64 ubuntu:22.04
ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
# System dependencies installation. Installs essential tools and libraries required for development and runtime (Required)
RUN apt update && apt install -y     wget     git     build-essential     libffi-dev     libtiff-dev     python3     python3-pip     python-is-python3     jq     curl     locales     locales-all     tzdata     && rm -rf /var/lib/apt/lists/*
# install patch (required)
RUN apt install -y patch
# Install package and environment manager. Downloads and sets up a lightweight environment management tool
RUN wget 'https://repo.anaconda.com/miniconda/Miniconda3-py311_23.11.0-2-Linux-x86_64.sh' -O miniconda.sh     && bash miniconda.sh -b -p /opt/miniconda3     && rm miniconda.sh
ENV PATH=/opt/miniconda3/bin:$PATH
RUN conda init --all     && conda config --append channels conda-forge
# Sets up a dedicated environment with specific dependencies for the target environemnt
RUN /bin/bash -c "source /opt/miniconda3/etc/profile.d/conda.sh &&     conda create -n testbed python=3.7 -y &&     conda activate testbed &&     pip install pytest==6.2.5 typing_extensions==3.10"
# set default workdir to testbed. (Required)
WORKDIR /testbed/
# Target Project setup. Clones source code, checkouts to the taget version, configures it, and installs project-specific dependencies
RUN /bin/bash -c "source /opt/miniconda3/etc/profile.d/conda.sh &&     conda activate testbed &&     git clone https://github.com/python/mypy /testbed &&     chmod -R 777 /testbed &&     cd /testbed &&     git reset --hard 6de254ef00f99ce5284ab947f2dd1179db6d28f6 &&     git remote remove origin &&     pip install -r test-requirements.txt &&     pip install -e ."
RUN echo "source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed" >> /root/.bashrc
</dockerfile>
"""


DOCKERFILE_USER_PROMPT_INIT_UBUNTU_ONLY = """Generate a **Dockerfile** based on the collected environment setup information.
The Dockerfile must ensure that the provided test files can be executed correctly.

### **Requirements:**
1. **Clone the repository** inside the Docker container into `/testbed/` and set `WORKDIR` to `/testbed/`.
2. **Checkout a specific commit SHA**, which will be provided by the user.
3. **Set up the environment** based on the information from the context retrieval agent:
   - Install necessary system dependencies and programming language versions.
   - Set up a virtual environment (`testbed`) if required.
   - Install all necessary libraries and dependencies.
4. **Ensure test execution** by setting up all necessary configurations.

### Important Notes:
1. You are FORBIDDEN to run tests in the dockerfile, tests will be run using eval script.
2. When building the Dockerfile, you MUST prioritize using package managers such as Conda, Maven, or NPM etc to set up the environment efficiently.
3. Ensure shell compatibility by using `/bin/bash` as the default shell environment to avoid runtime issues.  For example, **do not use `FROM alpine:latest`**, as it lacks `/bin/bash` by default, which may cause runtime errors. Instead, use a base image like `ubuntu:22.04` or `debian:bookworm` that includes Bash by default.
4. Pay more attention when using Ubuntu-based images**, as different versions may have variations in default packages, dependency resolution, and package manager behavior, which could lead to unexpected errors.
5. DO NOT use `COPY` to copy local files** into the Docker container.
   - For example, avoid using `COPY package.json /testbed/` or `COPY requirements.txt /testbed/`.
   - Instead, all files should be retrieved directly by **cloning the repository** inside the container to ensure a fully reproducible environment.
6. DO NOT run tests in the Dockerfile**.
   - Do not include commands like `npm test`, `pytest`, or `mvn test` in the Dockerfile.
   - Tests will be executed separately, and running them during the Docker build stage is an unnecessary overhead.
   - You can skip tests during environment setup because this is not your job.
7. If there is a reference Dockerfile, use it as a guideline.
8. Do not use ENTRYPOINT.
9. Please install necessary essential tools and libraries required for development and runtime, such as git etc.
10. Always install the target repository itself in development mode (`pip install -e .` for Python, `npm link` for Node.js, or `mvn install` for Java) so tests use the local cloned code, not a pre-built registry package.
   In addition, freely install any extra dependencies required by the tests (e.g. `pip install torch`, `pip install pytest`) using the package manager — these are environment dependencies, not the target package itself, and installing them from registries is correct and expected.
   **Do NOT** re-install the target repository package itself from a registry (e.g. `pip install black` when the repo IS black) as that would shadow the local code.
11. **You MUST use `ubuntu` image as the base image and manually install dependencies**, to avoid issues related to unavailable or broken images. This approach ensures that the Dockerfile builds successfully and the environment is properly set up. For example, you can use:
    ```dockerfile
    FROM ubuntu:xx.xx
    ```
    This helps avoid situations where the base image might not be available or is misconfigured, ensuring a reliable build process.

### **Example Format:**
The Dockerfile must be wrapped in `<dockerfile>` tags. Example:

<dockerfile>
# Base image specification. Defines the foundation OS and architecture for the container (Required)
FROM --platform=linux/x86_64 ubuntu:22.04
ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
# System dependencies installation. Installs essential tools and libraries required for development and runtime (Required)
RUN apt update && apt install -y     wget     git     build-essential     libffi-dev     libtiff-dev     python3     python3-pip     python-is-python3     jq     curl     locales     locales-all     tzdata     && rm -rf /var/lib/apt/lists/*
# install patch (required)
RUN apt install -y patch
# Install package and environment manager. Downloads and sets up a lightweight environment management tool
RUN wget 'https://repo.anaconda.com/miniconda/Miniconda3-py311_23.11.0-2-Linux-x86_64.sh' -O miniconda.sh     && bash miniconda.sh -b -p /opt/miniconda3     && rm miniconda.sh
ENV PATH=/opt/miniconda3/bin:$PATH
RUN conda init --all     && conda config --append channels conda-forge
# Sets up a dedicated environment with specific dependencies for the target environemnt
RUN /bin/bash -c "source /opt/miniconda3/etc/profile.d/conda.sh &&     conda create -n testbed python=3.7 -y &&     conda activate testbed &&     pip install pytest==6.2.5 typing_extensions==3.10"
# set default workdir to testbed. (Required)
WORKDIR /testbed/
# Target Project setup. Clones source code, checkouts to the taget version, configures it, and installs project-specific dependencies
RUN /bin/bash -c "source /opt/miniconda3/etc/profile.d/conda.sh &&     conda activate testbed &&     git clone https://github.com/python/mypy /testbed &&     chmod -R 777 /testbed &&     cd /testbed &&     git reset --hard 6de254ef00f99ce5284ab947f2dd1179db6d28f6 &&     git remote remove origin &&     pip install -r test-requirements.txt &&     pip install -e ."
RUN echo "source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed" >> /root/.bashrc
</dockerfile>
"""


DOCKERFILE_USER_PROMPT_MODIFY = """The previous Dockerfile attempt failed. Modify it based on the feedback above.

Rules:
- Keep the `FROM` line exactly as-is — do NOT change the base image.
- Do NOT add `git clone` — the repo is already at `/testbed`.
- Do NOT run tests inside the Dockerfile.
- Do NOT repeat steps already done by the base image (refer to the base Dockerfile content shown earlier).

Return the corrected Dockerfile wrapped in <dockerfile></dockerfile>.
"""


# ===========================================================================
# 2. DOCKERFILE AGENT — INSTANCE-LAYER MODE (multi-layer build)
# ===========================================================================

DOCKERFILE_INSTANCE_LAYER_SYSTEM_PROMPT = """You are an Instance Dockerfile Agent. Generate a minimal Dockerfile that layers on top of a pre-built base image to prepare the environment for a specific commit.

## Rules

- **First line MUST be `FROM <base_image>`** — the exact tag provided. Never use `python:*`, `ubuntu:*`, or any other image.
- **Do NOT repeat anything already in the base Dockerfile** — the user prompt shows the exact base Dockerfile content. Everything in it is already done.
- **Do NOT `git clone`** — the repo is already at `/testbed`.
- Only add what is strictly necessary for the target commit: `git checkout`, `git clean -fd`, and re-syncing deps if they changed.

## Output

Wrap the Dockerfile in `<dockerfile>` tags. Return ONLY the Dockerfile — no explanation.

If the test_analysis_agent provided feedback on a previous attempt, apply it precisely."""


DOCKERFILE_INSTANCE_LAYER_USER_PROMPT = """Generate a minimal **instance-layer Dockerfile** that builds on top of the pre-built base image.

## Base Image: `{base_image}`

The following is the **exact Dockerfile used to build `{base_image}`**.
Everything in it is ALREADY done — do NOT repeat any of these steps.

```dockerfile
{base_dockerfile_content}
```

- **Main package name**: {main_package} (used for the verification import check)

## Instance Info

- **Instance ID**: `{instance_id}`
- **Base commit**: `{base_commit}` (the state BEFORE the fix — bug is present here)

## Dependency Files at Base Commit

### pyproject.toml / setup.py (at base_commit)
```
{dep_file_content}
```

## Files Changed by the Fix (for context only — helps you spot if deps changed)

```
{patch_files_list}
```

## Gold Patch (for context only)

```diff
{patch}
```

Generate the Dockerfile now. Wrap it in `<dockerfile>` tags.

Example structure (follow this exactly — do NOT change the base image or add git clone):

<dockerfile>
# REQUIRED: inherit from the pre-built base image — do NOT change this line
FROM {base_image}

WORKDIR /testbed

# DO NOT git clone — repo already exists at /testbed

# 1. Checkout to the target commit (bug still present)
RUN cd /testbed && git checkout {base_commit} && git clean -fd

# 2. Re-install project at this commit (always — setup.py may have changed)
RUN cd /testbed && pip install --no-cache-dir -e .

# 3. Verify the environment works
RUN cd /testbed && python -c "import {main_package}; print('OK')"

# 4. Install any extra dependencies unique to this commit (add only if needed)
# RUN pip install some-extra-package==1.2.3
</dockerfile>
"""


def _load_base_dockerfile(base_image: str) -> str:
    """Load the base Dockerfile content for the given image tag from docker/.

    Derives the filename from _REPO_ENV_CONFIG by matching the base image tag.
    Returns a placeholder string if the file cannot be found or read.
    """
    for _tag, dockerfile_name, _ in _REPO_ENV_CONFIG.values():
        if _tag == base_image:
            dockerfile_path = _DOCKER_DIR / dockerfile_name
            try:
                return dockerfile_path.read_text(encoding="utf-8").rstrip()
            except OSError:
                return f"(could not read {dockerfile_path})"
    return "(base Dockerfile not found for this image — refer to the repo-specific environment template)"


def get_dockerfile_instance_layer_user_prompt(
    base_image: str,
    base_commit: str,
    main_package: str = "",
    instance_id: str = "",
    dep_file_content: str = "same as base image",
    patch_files_list: str = "",
    patch: str = "",
) -> str:
    return DOCKERFILE_INSTANCE_LAYER_USER_PROMPT.format(
        base_image=base_image,
        base_commit=base_commit,
        main_package=main_package,
        instance_id=instance_id,
        dep_file_content=dep_file_content,
        patch_files_list=patch_files_list,
        patch=patch,
        base_dockerfile_content=_load_base_dockerfile(base_image),
    )


def get_dockerfile_system_prompt(instance_layer: bool = False) -> str:
    if instance_layer:
        return DOCKERFILE_INSTANCE_LAYER_SYSTEM_PROMPT
    return DOCKERFILE_SYSTEM_PROMPT


def get_dockerfile_user_prompt_init(ubuntu_only: bool = False) -> str:
    if ubuntu_only:
        return DOCKERFILE_USER_PROMPT_INIT_UBUNTU_ONLY
    return DOCKERFILE_USER_PROMPT_INIT


def get_dockerfile_user_prompt_modify() -> str:
    return DOCKERFILE_USER_PROMPT_MODIFY


# ===========================================================================
# 3. DOCKERFILE AGENT — REPO-SPECIFIC ENVIRONMENT TEMPLATES
# ===========================================================================

# Maps repo name → (base image tag, base Dockerfile filename, instance-layer guidance)
_REPO_ENV_CONFIG: dict[str, tuple[str, str, str]] = {
    "MiroMindAI/miroflow": (
        "swe-factory/miroflow:base",
        "Dockerfile.miroflow",
        """\
## What the instance-layer Dockerfile MUST do (and nothing else)
```dockerfile
FROM swe-factory/miroflow:base
WORKDIR /testbed
RUN git checkout <commit> && git clean -fd
RUN uv sync                              # re-syncs if deps changed at this commit
RUN uv pip install pytest pytest-asyncio # ensure test deps present
```
Do NOT run `uv venv` — `.venv` already exists. Do NOT git clone again.
Do NOT add `ENV PATH=...` — call pytest via `.venv/bin/pytest` (see eval.sh pattern).

## Project layout & import paths
- pyproject.toml: `[tool.hatch.build.targets.wheel] packages = ["src"]`
- This means `src/` IS the package root — there is NO `miroflow` top-level package.
- CORRECT imports: `from core.foo import Bar`, `from llm.client import X`, `from utils.helper import Y`
- WRONG imports: `from src.core.foo import Bar`, `from miroflow.core.foo import Bar`

## eval.sh pattern (MUST follow exactly)
```bash
#!/bin/bash
set -uxo pipefail
cd /testbed
mkdir -p tests                           # ensure test dir exists before patch apply
git apply --no-index -v - <<'EOF_PATCH'
[CONTENT OF TEST PATCH]
EOF_PATCH
.venv/bin/pytest tests/ -v
rc=$?
echo "OMNIGRIL_EXIT_CODE=$rc"
```
IMPORTANT: Use `.venv/bin/pytest` directly — `uv run pytest` may create a new venv and lose installed packages.
""",
    ),

    "MiroMindAI/MiroThinker": (
        "swe-factory/mirothinker:base",
        "Dockerfile.mirothinker",
        """\
## What the instance-layer Dockerfile MUST do (and nothing else)
```dockerfile
FROM swe-factory/mirothinker:base
WORKDIR /testbed
RUN git checkout <commit> && git clean -fd
WORKDIR /testbed/apps/miroflow-agent
RUN uv sync                              # re-syncs if deps changed; does NOT recreate venv
```
CRITICAL: Do NOT run `uv venv` — the `.venv` already exists at `/testbed/apps/miroflow-agent/.venv`.
Do NOT add `ENV PATH=...` — use `uv run pytest` to invoke pytest.

## Project layout & import paths
- `apps/miroflow-agent/pyproject.toml`: `[tool.hatch.build.targets.wheel] packages = ["src"]`
- This means `src/` IS the package root relative to `apps/miroflow-agent/`.
- CORRECT imports: `from core.foo import Bar`, `from llm.client import X`
- WRONG imports: `from src.core.foo import Bar`, `from miroflow.core.foo import Bar`
- Monorepo: `apps/miroflow-agent/` is the main app, `libs/miroflow-tools/` is the lib
- Key deps: anthropic, openai, mcp, fastmcp, e2b-code-interpreter, hydra-core, transformers

## eval.sh pattern (MUST follow exactly)
```bash
#!/bin/bash
set -uxo pipefail
cd /testbed/apps/miroflow-agent
mkdir -p tests                           # ensure test dir exists before patch apply
git apply --no-index -v - <<'EOF_PATCH'
[CONTENT OF TEST PATCH]
EOF_PATCH
.venv/bin/pytest tests/ -v
rc=$?
echo "OMNIGRIL_EXIT_CODE=$rc"
```
IMPORTANT: Use `.venv/bin/pytest` directly — `uv run pytest` may create a new venv and lose installed packages.
""",
    ),

    "MiroMindAI/sd-torchtune": (
        "swe-factory/sd-torchtune:base",
        "Dockerfile.sd-torchtune",
        """\
## What the instance-layer Dockerfile MUST do (and nothing else)
```dockerfile
FROM swe-factory/sd-torchtune:base
WORKDIR /testbed
RUN git checkout <commit> && git clean -fd
# Re-apply the torchao compatibility fix after checkout (git clean resets tracked files)
RUN sed -i 's/from torchao.utils import TORCH_VERSION_AFTER_2_4/from torchao.utils import torch_version_at_least; TORCH_VERSION_AFTER_2_4 = torch_version_at_least("2.4.0")/' /testbed/tests/recipes/test_configs.py
RUN pip install --no-cache-dir -e ".[dev]"
```
Do NOT reinstall torch, torchao, or transformers — already in base image.
Do NOT modify the flash_attn stub — already correctly set up in base image.
GITHUB_TOKEN is NOT needed in the instance-layer (already used in base build).

## Project layout & import paths
- setuptools project; packages: `torchtune` and `recipes` at repo root
- CORRECT imports: `from torchtune.xxx import Y`, `from recipes.xxx import Y`
- WRONG imports: `from src.torchtune.xxx import Y`
- 159 existing test files under `tests/`
- Key deps: torchdata, liger-kernel, datasets, huggingface_hub, safetensors, sentencepiece, tiktoken

## eval.sh pattern (MUST follow exactly)
```bash
#!/bin/bash
set -uxo pipefail
cd /testbed
mkdir -p <parent_dir_of_test_file>      # ensure test dir exists before patch apply
git apply --no-index -v - <<'EOF_PATCH'
[CONTENT OF TEST PATCH]
EOF_PATCH
pytest tests/ -v --without-integration
rc=$?
echo "OMNIGRIL_EXIT_CODE=$rc"
```
IMPORTANT: Use `pytest` directly (it is on PATH via pip). Do NOT use `uv run`. Do NOT source activate.
""",
    ),
}

# Resolve docker/ directory relative to this file so it works from any cwd.
_DOCKER_DIR = pathlib.Path(__file__).parent.parent.parent / "docker"


def get_repo_env_template(repo_name: str) -> str:
    """Return repo-specific env template string, or empty string if not found.

    The base-image section is loaded live from docker/<Dockerfile> so the
    prompt always reflects the actual file on disk.
    """
    config = _REPO_ENV_CONFIG.get(repo_name)
    if config is None:
        return ""

    base_tag, dockerfile_name, instance_guidance = config
    dockerfile_path = _DOCKER_DIR / dockerfile_name

    try:
        base_dockerfile_content = dockerfile_path.read_text(encoding="utf-8").rstrip()
    except OSError:
        base_dockerfile_content = f"(could not read {dockerfile_path})"

    return (
        f"### Repo Environment: {repo_name}\n\n"
        f"## Base image `{base_tag}` — raw Dockerfile content\n"
        f"Everything below is ALREADY done in the base image. "
        f"Do NOT repeat it in the instance-layer Dockerfile.\n\n"
        f"```dockerfile\n{base_dockerfile_content}\n```\n\n"
        f"{instance_guidance}"
    )


# ===========================================================================
# 4. EVAL SCRIPT AGENT
# ===========================================================================

EVAL_SCRIPT_SYSTEM_PROMPT = """You are a software agent specialized in writing evaluation scripts to run tests inside a Docker environment.
Your task is to generate an **evaluation script** that executes the given test files in the prepared Docker environment.

You will receive the following information:
- **Collected environment details** from the **context retrieval agent** (if available), including dependencies, test execution commands, and any special setup steps.
- **The generated Dockerfile** that defines the environment in which the tests will be executed.
- **A list of test files** that must be executed, provided by the user.
- **An evaluation script skeleton (`eval_script_skeleton`) that MUST be followed.
- Guidance from the **test_analysis_agent** (if available). This agent will run your evaluation script and, if it finds any issues, provide specific feedback or guidance for you to improve the script.

### Your Responsibilities:
1. Ensure the evaluation script properly activates the environment inside the Docker container.
2. Apply the test patch (if needed) before executing the tests.
3. When available, use the correct test execution commands and setup steps collected by the context retrieval agent.
4. If guidance from the test_analysis_agent is provided, update or improve your evaluation script according to its suggestions.

The generated script must follow best practices, ensuring all necessary steps are performed to successfully run the tests."""


EVAL_SCRIPT_USER_PROMPT_INIT = """Generate an **evaluation script** based on the collected environment setup and test execution information.
The script must execute the provided test files inside the specified Docker environment.

### **Requirements:**
1. **Activate the environment**: Ensure the correct environment (e.g., Conda, venv) is activated before running the tests.
2. **Apply the test patch (if required)**: The test patch may need to be applied before running the tests.
3. **Execute the given test files** using the correct command found by the context retrieval agent.
4. **Ensure proper cleanup**: After running the tests, any modified files should be reset.

### Important Notes:
1. You must **execute only the specified target test files**, rather than running all tests in the repository.
   - Running all tests can be highly time-consuming and unnecessary.
   - Ensure that only the **required test cases** are executed based on the provided test file list.

2. **Optimize execution efficiency by combining multiple test commands into a single command** whenever possible.
   - Avoid running multiple separate test commands if they can be executed in one batch.
   - This reduces redundant initialization overhead and speeds up execution.

3. **Ensure that the output of the evaluation script is concise and structured**, making it easier for the **test log analysis agent** to process.
   - The test command **must output the names and pass/fail/skip status of each target executed test file**.
   - Avoid excessive debug information or unrelated output in eval script, but **do not suppress key test execution details**.
   - Avoid running all tests! **Just run the target test files**.

4. **Follow the structure of the reference evaluation script or eval script skeleton whenever available.**
   - Use **a simple, minimalistic structure** similar to the reference eval script to ensure clarity and maintainability.
   - The script should be easy to modify and extend without unnecessary complexity.

5. **The actual test patch content is omitted here for brevity (marked with [CONTENT OF TEST PATCH] placeholder).**
    - You must generate the complete git apply command structure, including the heredoc syntax with delimiter (EOF_114329324912).
    - The placeholder will be programmatically replaced with the actual patch content during script execution.
    - Example structure:
    git apply --no-index -v - <<'EOF_114329324912'\\n[CONTENT OF TEST PATCH]\\nEOF_114329324912

6. You MUST capture the exit code immediately after running the tests using `rc=$?`, and then echo: `OMNIGRIL_EXIT_CODE=$rc`. This ensures the judge can determine whether the tests passed successfully.

Eval script skeleton:
{eval_script_skeleton}

### **Example Format:**
The script must be wrapped in `<script>` tags. Example:

<script>
#!/bin/bash
set -uxo pipefail
source /opt/miniconda3/bin/activate
conda activate testbed
cd /testbed
pip install -r test-requirements.txt && pip install -e .

git checkout 6de254ef00f99ce5284ab947f2dd1179db6d28f6 "test-data/unit/check-functions.test" "test-data/unit/check-redefine.test"

# Required: apply test patch to update target tests
git apply --no-index -v - <<'EOF_114329324912'
[CONTENT OF TEST PATCH]
EOF_114329324912

# Required: run target tests files instead of all tests!
pytest --no-header -rA --tb=no -p no:cacheprovider -n4 mypy/test/testcheck.py::TypeCheckSuite::check-functions.test mypy/test/testcheck.py::TypeCheckSuite::check-redefine.test
rc=$?            #Required, save exit code
echo "OMNIGRIL_EXIT_CODE=$rc" #Required, echo test status
git checkout 6de254ef00f99ce5284ab947f2dd1179db6d28f6 "test-data/unit/check-functions.test" "test-data/unit/check-redefine.test"
</script>
"""


EVAL_SCRIPT_USER_PROMPT_INIT_WITH_DOWNLOADS = """Generate an **evaluation script** based on the collected environment setup and test execution information.
The script must execute the provided test files inside the specified Docker environment.

### **Requirements:**
1. **Activate the environment**: Ensure the correct environment (e.g., Conda, venv) is activated before running the tests.
2. **Apply the test patch (if required)**: The test patch may need to be applied before running the tests.
3. **Execute the given test files** using the correct command found by the context retrieval agent.
4. **Ensure proper cleanup**: After running the tests, any modified files should be reset.

### Important Notes:
1. You must **execute only the specified target test files**, rather than running all tests in the repository.
2. **Optimize execution efficiency by combining multiple test commands into a single command** whenever possible.
3. **Ensure that the output of the evaluation script is concise and structured**.
4. **Follow the structure of the reference evaluation script or eval script skeleton whenever available.**
5. **The actual test patch content is omitted here for brevity (marked with [CONTENT OF TEST PATCH] placeholder).**
    - You must generate the complete git apply command structure, including the heredoc syntax with delimiter (EOF_114329324912).
    - The placeholder will be programmatically replaced with the actual patch content during script execution.
6. You MUST capture the exit code immediately after running the tests using `rc=$?`, and then echo: `OMNIGRIL_EXIT_CODE=$rc`.
7. Test resources to download/remove:
    - For each resource that needs to be added, use wget with the -O <path> flag to download it directly into its target location.
    - For each resource that needs to be removed, issue a `rm -f <path>` command.
    - Integrate these download/remove commands immediately after resetting the tests.

Eval script skeleton:
{eval_script_skeleton}

### **Example Format:**
The script must be wrapped in `<script>` tags. Example:

<script>
#!/bin/bash
set -uxo pipefail
source /opt/miniconda3/bin/activate
conda activate testbed
cd /testbed
pip install -r test-requirements.txt && pip install -e .

git checkout 6de254ef00f99ce5284ab947f2dd1179db6d28f6 "test-data/unit/check-functions.test" "test-data/unit/check-redefine.test"

# Required: download and remove test_resources
wget -O /testbed/test/xmp_no_prefix.jpg https://raw.githubusercontent.com/owner/python/mypy/xxxx/head/test/xmp_no_prefix.jpg || exit 1
rm -f /testbed/test/xmp_no_prefix_old.jpg

# Required: apply test patch to update target tests
git apply --no-index -v - <<'EOF_114329324912'
[CONTENT OF TEST PATCH]
EOF_114329324912

# Required: run target tests files instead of all tests!
pytest --no-header -rA --tb=no -p no:cacheprovider -n4 mypy/test/testcheck.py::TypeCheckSuite::check-functions.test mypy/test/testcheck.py::TypeCheckSuite::check-redefine.test
rc=$?            #Required, save exit code
echo "OMNIGRIL_EXIT_CODE=$rc" #Required, echo test status
git checkout 6de254ef00f99ce5284ab947f2dd1179db6d28f6 "test-data/unit/check-functions.test" "test-data/unit/check-redefine.test"
</script>
"""


def get_eval_script_system_prompt() -> str:
    return EVAL_SCRIPT_SYSTEM_PROMPT


def get_eval_script_user_prompt_init(eval_script_skeleton: str, with_downloads: bool = False) -> str:
    if with_downloads:
        return EVAL_SCRIPT_USER_PROMPT_INIT_WITH_DOWNLOADS.format(eval_script_skeleton=eval_script_skeleton)
    return EVAL_SCRIPT_USER_PROMPT_INIT.format(eval_script_skeleton=eval_script_skeleton)


# ===========================================================================
# 5. WRITE TEST AGENT
# ===========================================================================

_TEST_SHARED_FILE_FORMAT = """
### Output Format:
Return each test file as its **complete raw content** wrapped in `<test_file path="...">` tags.
Use the `path` attribute to specify where the file should be placed relative to the repo root (e.g. `tests/test_my_fix.py`).
Use comments in the test files to clearly mark F2P vs P2P tests, e.g.:
  # --- F2P: tests that should fail before patch, pass after ---
  # --- P2P: regression tests that should always pass ---

Example format (one tag per file):
```
<test_file path="tests/test_my_fix.py">
import pytest
from mymodule import my_function

def test_f2p_returns_correct_value():
    result = my_function(42)
    assert result == expected_value
</test_file>
```

You may emit multiple `<test_file ...>` blocks if you want to create more than one file.
Do NOT wrap the content in diff syntax — write plain source code only.
"""

TEST_SYSTEM_PROMPT_PYTHON = """You are a Test Agent for SWE-bench instance construction. Write pytest test files that validate a code change.

The Docker environment is already built — the repo is at `/testbed` at the correct commit with all dependencies installed.

## Two test categories (both required)

**F2P (Fail-to-Pass)** — prefix `test_f2p_` — **write 3–4 tests**:
- FAIL on `base_commit` (before fix/feature)
- PASS after the gold patch is applied
- Prove the bug existed OR the feature was missing

**P2P (Pass-to-Pass)** — prefix `test_p2p_` — **write 3–4 tests**:
- PASS both before and after the patch
- Prove the change doesn't break related functionality

## Rules

- **Classify first**: is the patch a bug fix (wrong output) or new feature (missing attribute/param)?
  - Bug fix → assert the EXACT correct value the fixed code returns
  - New feature → call the new API; test naturally fails pre-patch (AttributeError/TypeError) and passes post-patch
- **No over-mocking**: never mock the function/class the patch is fixing; only mock external I/O
- **Concrete assertions**: `assert result == specific_value`, not `assert result` or `assert result is not None`
- **Import paths**: use the module path relative to the package root, not to the repo root. The repo environment template (provided separately) specifies the exact import style for this repo.
- **Relevance**: only test code mentioned in the patch
- Apply any guidance from the test_analysis_agent precisely
""" + _TEST_SHARED_FILE_FORMAT

TEST_SYSTEM_PROMPT_JAVASCRIPT = """You are a Test Agent for SWE-bench instance construction. Your ONLY job is to write JavaScript test files (using Jest or Mocha) that validate whether a bug fix has been correctly applied.

A Docker environment has already been built and verified. Focus entirely on writing high-quality tests.

You will receive:
- **Problem statement**: The description of the issue or feature being addressed.
- **Patch content**: The code changes (unified diff) made to resolve the issue.
- **Repository info**: Basic information about the target repository.
- **Guidance** (if available): Feedback from a test analysis agent on how to improve previously generated tests.

Generate **two categories** of tests:
- **F2P tests** (prefix `test_f2p_` or describe block "F2P"): FAIL before patch, PASS after.
- **P2P tests** (prefix `test_p2p_` or describe block "P2P"): PASS both before and after.

Do NOT mock the function the patch fixes. Test observable behavior, not implementation details.
""" + _TEST_SHARED_FILE_FORMAT + """
Example:
<test_file path="tests/fix_issue.test.js">
const { myFunction } = require('../src/myModule');

// --- F2P: tests that should fail before patch, pass after ---
describe('myFunction fix', () => {
  test('test_f2p_returns correct value after fix', () => {
    expect(myFunction(42)).toBe(expectedValue);
  });
});

// --- P2P: regression tests that should always pass ---
describe('myFunction regression', () => {
  test('test_p2p_handles edge case', () => {
    expect(myFunction(0)).not.toBeNull();
  });
});
</test_file>
"""

TEST_SYSTEM_PROMPT_JAVA = """You are a Test Agent for SWE-bench instance construction. Your ONLY job is to write Java JUnit test files that validate whether a bug fix has been correctly applied.

A Docker environment has already been built and verified. Focus entirely on writing high-quality tests.

You will receive:
- **Problem statement**: The description of the issue or feature being addressed.
- **Patch content**: The code changes (unified diff) made to resolve the issue.
- **Repository info**: Basic information about the target repository.
- **Guidance** (if available): Feedback from a test analysis agent on how to improve previously generated tests.

Generate **two categories** of tests:
- **F2P tests** (method names prefixed `testF2p`): FAIL before patch, PASS after.
- **P2P tests** (method names prefixed `testP2p`): PASS both before and after.

Do NOT mock the method the patch fixes. Test observable behavior.
""" + _TEST_SHARED_FILE_FORMAT + """
Example:
<test_file path="src/test/java/com/example/FixIssueTest.java">
package com.example;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class FixIssueTest {
    // --- F2P: tests that should fail before patch, pass after ---
    @Test
    void testF2pReturnsCorrectValue() {
        assertEquals(expectedValue, MyClass.myMethod(42));
    }

    // --- P2P: regression tests that should always pass ---
    @Test
    void testP2pHandlesEdgeCase() {
        assertNotNull(MyClass.myMethod(0));
    }
}
</test_file>
"""

TEST_SYSTEM_PROMPT_TYPESCRIPT = """You are a Test Agent for SWE-bench instance construction. Your ONLY job is to write TypeScript test files (using Jest or Vitest) that validate whether a bug fix has been correctly applied.

A Docker environment has already been built and verified. Focus entirely on writing high-quality tests.

You will receive:
- **Problem statement**: The description of the issue or feature being addressed.
- **Patch content**: The code changes (unified diff) made to resolve the issue.
- **Repository info**: Basic information about the target repository.
- **Guidance** (if available): Feedback from a test analysis agent on how to improve previously generated tests.

Generate **two categories** of tests:
- **F2P tests**: FAIL before patch, PASS after.
- **P2P tests**: PASS both before and after.

Do NOT mock the function the patch fixes. Test observable behavior.
""" + _TEST_SHARED_FILE_FORMAT + """
Example:
<test_file path="tests/fix_issue.test.ts">
import { myFunction } from '../src/myModule';

// --- F2P: tests that should fail before patch, pass after ---
describe('myFunction fix', () => {
  it('test_f2p_returns correct value after fix', () => {
    expect(myFunction(42)).toBe(expectedValue);
  });
});

// --- P2P: regression tests that should always pass ---
describe('myFunction regression', () => {
  it('test_p2p_handles edge case', () => {
    expect(myFunction(0)).not.toBeNull();
  });
});
</test_file>
"""


def get_test_system_prompt(language: str) -> str:
    """Select the language-specific system prompt for test generation."""
    lang = (language or "").lower().strip()
    if lang in ("javascript", "js", "nodejs"):
        return TEST_SYSTEM_PROMPT_JAVASCRIPT
    elif lang in ("java",):
        return TEST_SYSTEM_PROMPT_JAVA
    elif lang in ("typescript", "ts"):
        return TEST_SYSTEM_PROMPT_TYPESCRIPT
    return TEST_SYSTEM_PROMPT_PYTHON


TEST_USER_PROMPT = """Generate test files for the following pull request.

## Instance Info
- **Instance ID**: `{instance_id}`
- **Base commit**: `{base_commit}`

## Problem Statement
```
{problem_statement}
```

## Gold Patch
```diff
{patch_content}
```

## Existing Tests (match this import style if provided)
{existing_tests}

---

**Before writing**, reason through these steps:
1. Is this a **bug fix** or **new feature**? (Does the patch add new functions/params, or fix existing logic?)
2. Which exact function/method/class does the patch modify or add?
3. At `base_commit`, what does that code do for a concrete input? (wrong value, crash, AttributeError, TypeError?)
4. After the patch, what does it return/do for that same input?
5. What assertion FAILS on pre-patch behavior and PASSES on post-patch behavior?

Then generate test file(s) using `<test_file path="...">` tags.
You MUST produce **3–4 F2P tests** and **3–4 P2P tests**.
"""


TEST_REFLEXION_CRITIQUE_PROMPT = """Review the following auto-generated tests for quality.

### Problem Statement:
{problem_statement}

### Gold Patch:
{patch_content}

### Generated Tests:
{test_patch}

### Review each F2P test — answer all three questions:
1. What does the code at `base_commit` actually do for the test's input? (trace the pre-patch logic)
2. Does the assertion FAIL on that pre-patch behavior? (if not, the test is too weak)
3. Does the assertion PASS after the patch? (expected value must match post-patch output)

### Also check:
- **Count**: are there 3–4 F2P tests and 3–4 P2P tests? Flag if fewer than 3 of either category.
- **Over-mocking**: does any F2P test mock the exact function the patch fixes? → broken, must call real code
- **Weak assertions**: `assert result is not None`, `assert result`, `assert len > 0` → flag and suggest exact value
- **Wrong target**: does the test call a different function than what the patch changes? → fix the import/call
- **Import paths**: for monorepos, hyphens in dir names are invalid; strip subdirectory prefix correctly
- **P2P validity**: would P2P tests pass on the pre-patch code too?

If all F2P tests would genuinely fail pre-patch and pass post-patch, say "TESTS_APPROVED".
Otherwise provide specific, actionable fixes for each issue found.
"""

TEST_REFLEXION_REFINE_PROMPT = """Fix the tests based on the critique below. Address every issue raised.

### Critique:
{critique}

### Current Tests:
{test_patch}

### Problem Statement:
{problem_statement}

### Gold Patch:
{patch_content}

For each F2P test you write, confirm:
- Pre-patch: the assertion FAILS (buggy output or missing feature)
- Post-patch: the assertion PASSES (correct output or feature works)

Use concrete assertions (`assert result == exact_value`). Generate improved test files using `<test_file path="...">` tags.
"""


# ===========================================================================
# 6. CONTEXT RETRIEVAL AGENT
# ===========================================================================

CONTEXT_RETRIEVAL_SYSTEM_PROMPT = """You are a context_retrieval_agent responsible for gathering **precise and necessary information** from the local repository to support environment setup and test execution. After gathering the information, you will **generate a concise report** summarizing the key findings related to the setup and test execution.

Sometimes, another agent (such as a test analysis agent) may explicitly request specific information to help fix issues like Dockerfile errors or evaluation script failures.

Your primary goal is to:

- **If a specific request is provided by a calling agent, focus your retrieval narrowly on that request, extracting only the explicitly required files or data.**
- **If no explicit request is given by another agent, or if the request is incomplete or unclear, perform a basic and limited exploration of the repository to collect general environment and test execution information. Avoid exhaustive or in-depth searches.**
- **Pay special attention to the following information when collecting and summarizing:**
  - **Exact versions** of dependencies, libraries, and programming languages (e.g., `flask==2.0.3`, `python3.9`, `node 18`)
  - **Commands** for setting up the environment and executing tests (e.g., `pip install -r requirements.txt`, `pytest tests/test_api.py`)
  - Any environment configuration details (e.g., `.env` files, specific OS package dependencies, etc.)
  - Specific test commands for individual or specific test files, not just generic test execution commands.

### Important Notes:
- The repository has already been **cloned locally**; you are working within the local repository directory.
- You are **not expected to search broadly**; retrieve only the files and information explicitly requested by the calling agent.
- Avoid redundant or speculative searches — **be goal-driven and cost-efficient**.
- It is **common for this benchmark that no tests or test configs exist in the repo**. If you do not find tests, **state that clearly and stop searching**. Do **not** keep hunting for `pytest.ini`, `tox.ini`, or `conftest.py` after an initial check.
"""

CONTEXT_RETRIEVAL_USER_PROMPT = (
    "Your task is to gather sufficient context from the repository and external sources to understand how to set up the project's environment. To achieve this, you can use the following APIs to browse and extract relevant information:"
    "\n- browse_folder(path: str, depth: str): Browse and return the folder structure for a given path in the repository.  The depth is a string representing a number of folder levels to include in the output such as ``1''."
    "\n- browse_file_for_environment_info(file_path: str, custom_query: str): Call an agent to browse a file such as README or CONTRIBUTING.md and extract environment setup and running tests information. Use the `custom_query` parameter to tell the agent any extra details it should pay special attention to (for example, 'what java version do we need?')."
    "\n- search_files_by_keyword(keyword: str): Search for files in the repository whose names contain the given keyword."
    "\n\nYou may invoke multiple APIs in one round as needed to gather the required information."
    "\n\nNow analyze the repository and use the necessary APIs to gather the information required to understand and set up the environment. If you cannot find tests or test configs after a quick, minimal check, report that and stop. Ensure each API call has concrete arguments as inputs."
)
