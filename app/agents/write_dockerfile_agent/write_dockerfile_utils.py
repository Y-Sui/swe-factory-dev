"""
An agent, which is only responsible for the write_dockerfile tool call.
"""

import json
from collections.abc import Callable
from os.path import join as pjoin
from pathlib import Path
import os
from loguru import logger
from app.data_structures import MessageThread, MethodId
from app.log import print_acr, print_patch_generation
from app.model import common
from app.task import Task
import re


SYSTEM_PROMPT_DOCKERFILE = """You are a software agent specialized in creating Docker environments for software projects.  
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



USER_PROMPT_INIT_DOCKERFILE = """Generate a **Dockerfile** based on the collected environment setup information.  
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
10. When setting up dependencies for the target repository (e.g., `torch 3.33`), **DO NOT** install the package directly from external registries (e.g., PyPI, NPM, Maven Central) using commands like `pip install <package>` (e.g., `pip install torch`).  
   Instead, **you can install the repository itself in development mode** (`pip install -e .` for Python, `npm link` for Node.js, or `mvn install` for Java) to ensure that the local repository’s code is correctly referenced during execution.
   **Why is this important?**  
   - If you modify the repository’s source code but have already installed a pre-built package from the registry, your system may load the installed package instead of your local code, **leading to incorrect test results and making debugging difficult**.  
   - Using development mode installation (`pip install -e .`, `npm link`, `mvn install`) ensures that the system always references the latest local repository code, preventing version mismatches and ensuring that modifications are properly reflected in subsequent tests.
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

USER_PROMPT_INIT_DOCKERFILE_USE_UBUNTU_ONLY = """Generate a **Dockerfile** based on the collected environment setup information.  
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
10. When setting up dependencies for the target repository (e.g., `torch 3.33`), **DO NOT** install the package directly from external registries (e.g., PyPI, NPM, Maven Central) using commands like `pip install <package>` (e.g., `pip install torch`).  
   Instead, **you can install the repository itself in development mode** (`pip install -e .` for Python, `npm link` for Node.js, or `mvn install` for Java) to ensure that the local repository’s code is correctly referenced during execution.
   **Why is this important?**  
   - If you modify the repository’s source code but have already installed a pre-built package from the registry, your system may load the installed package instead of your local code, **leading to incorrect test results and making debugging difficult**.  
   - Using development mode installation (`pip install -e .`, `npm link`, `mvn install`) ensures that the system always references the latest local repository code, preventing version mismatches and ensuring that modifications are properly reflected in subsequent tests.
11. **You MUST use `ubuntu` image as the base image and manually install dependencies **, to avoid issues related to unavailable or broken images. This approach ensures that the Dockerfile builds successfully and the environment is properly set up. For example, you can use:
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

USER_PROMPT_MODIFY_DOCKERFILE = """Please modify current dockerfile according to collected information. 
Important Notes:
1. If the Dockerfile is building a project that is itself a PyPI package (e.g., black, flake8, mypy, etc.), and the repository is cloned and installed with `pip install -e .`, then:
- **Do NOT pre-install the same package from PyPI** using `pip install black` or similar. This is redundant and can lead to version conflicts or incorrect test behavior.
- Always assume the cloned repo is the authoritative source of truth.

2. **Do NOT run tests directly inside the Dockerfile** (e.g., avoid adding `RUN pytest` or `RUN make test` inside the Dockerfile):
- Testing should be performed **after** the image is built (in CI pipeline or post-build validation step), not during image creation.
- Embedding tests in the Dockerfile breaks caching and slows down builds.

3. If you frequently encounter issues with the base image, consider using FROM ubuntu:xx.xx and manually installing dependencies (node,maven,java,python,etc.) to ensure a stable and reliable environment.

Return modified dockerfile in defined format. Wrap results in <dockerfile></dockerfile>.
"""


REPO_ENV_TEMPLATES = {
    "MiroMindAI/miroflow": """### Repo Environment: MiroMindAI/miroflow
- Language: Python >=3.12
- Build system: hatchling (`pyproject.toml`, `[build-system] requires = ["hatchling"]`)
- Package manager: uv ONLY — do NOT mix with `python3 -m venv` or `pip install`
- Install uv (IMPORTANT — use one of these two methods, do NOT use `pip install uv`):
  ```dockerfile
  # Method A (recommended): copy the uv binary directly from the official image
  COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

  # Method B: install script, installs to /usr/local/bin
  RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
  ```
- CRITICAL — use uv exclusively, never mix with python3 -m venv:
  `uv sync` creates and manages its own `.venv` automatically at the WORKDIR.
  If you also run `python3 -m venv venv`, you get TWO separate venvs and pytest
  ends up in `.venv` while PATH points to `venv` — pytest will not be found.
  Correct Dockerfile pattern:
  ```dockerfile
  WORKDIR /testbed
  RUN git clone https://github.com/MiroMindAI/miroflow . && git reset --hard <commit>
  COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
  RUN uv sync                              # creates /testbed/.venv automatically
  RUN uv pip install pytest pytest-asyncio # installs into /testbed/.venv
  ENV PATH="/testbed/.venv/bin:$PATH"
  ```
  eval.sh activation: `source /testbed/.venv/bin/activate`
- Test runner: pytest (no existing tests — LLM-generated tests will be placed in /testbed/tests/)
- Key deps: anthropic, openai, mcp, fastmcp, hydra-core, rich, fire, google-genai
- Entry point: main.py
- Recommended base: ubuntu:22.04 + deadsnakes PPA for Python 3.12
  ```dockerfile
  RUN apt update && apt install -y software-properties-common curl git
  RUN add-apt-repository ppa:deadsnakes/ppa && apt update
  RUN apt install -y python3.12 python3.12-venv python3.12-dev
  RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1
  ```
""",

    "MiroMindAI/MiroThinker": """### Repo Environment: MiroMindAI/MiroThinker
- Language: Python >=3.12
- Structure: Monorepo — `apps/` and `libs/` sub-packages
  - `libs/miroflow-tools/` — install first (editable)
  - `apps/miroflow-agent/` — main app (where most patches apply), depends on miroflow-tools
- Build system: hatchling for each sub-package
- Package manager: `uv` (preferred) or `pip`
- Install uv (IMPORTANT — use one of these two methods, do NOT use `pip install uv`):
  ```dockerfile
  # Method A (recommended): copy the uv binary directly from the official image
  COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

  # Method B: install script, installs to /usr/local/bin
  RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
  ```
- CRITICAL — venv location and uv usage:
  `uv sync` creates `.venv` at the CURRENT WORKDIR. You MUST set WORKDIR to
  `/testbed/apps/miroflow-agent` BEFORE running `uv venv` or `uv sync`, so the
  venv lands at `/testbed/apps/miroflow-agent/.venv`.
  Do NOT run `uv venv` or `uv sync` from `/testbed` — the venv will be at the
  wrong location and eval.sh will fail to activate it.
  Correct Dockerfile pattern (order matters):
  ```dockerfile
  COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
  WORKDIR /testbed
  RUN git clone https://github.com/MiroMindAI/MiroThinker . && git reset --hard <commit>
  # Install libs/miroflow-tools first using --system (no venv yet)
  RUN uv pip install --system -e libs/miroflow-tools
  # Now switch to the main app directory and create venv + sync there
  WORKDIR /testbed/apps/miroflow-agent
  RUN uv venv                              # creates /testbed/apps/miroflow-agent/.venv
  RUN uv sync                              # installs all deps into that venv
  RUN uv pip install pytest pytest-asyncio pytest-cov pytest-mock
  ENV PATH="/testbed/apps/miroflow-agent/.venv/bin:$PATH"
  ```
  eval.sh activation: `source /testbed/apps/miroflow-agent/.venv/bin/activate`
- WORKDIR for Dockerfile: `/testbed/apps/miroflow-agent` (this is where uv.lock and pyproject.toml live for the main app)
- Test runner: pytest + pytest-asyncio (async tests use `asyncio_mode = "auto"`)
  - Run from `/testbed/apps/miroflow-agent`: `pytest tests/ -v`
  - Import paths in tests are relative to `apps/miroflow-agent/` — e.g. patch file `apps/miroflow-agent/src/core/foo.py` → `from src.core.foo import Foo`
- Test deps: pytest, pytest-asyncio, pytest-cov, pytest-xdist, pytest-mock
- Key deps: anthropic, openai, mcp, fastmcp, e2b-code-interpreter, hydra-core, transformers
- Recommended base: ubuntu:22.04 + deadsnakes PPA for Python 3.12
  ```dockerfile
  RUN apt update && apt install -y software-properties-common curl git
  RUN add-apt-repository ppa:deadsnakes/ppa && apt update
  RUN apt install -y python3.12 python3.12-venv python3.12-dev
  RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1
  ```
  eval.sh pattern:
  ```bash
  cd /testbed/apps/miroflow-agent
  source /testbed/apps/miroflow-agent/.venv/bin/activate
  git apply --no-index -v - <<'EOF_114329324912'
  [CONTENT OF TEST PATCH]
  EOF_114329324912
  pytest tests/ -v
  rc=$?
  echo "OMNIGRIL_EXIT_CODE=$rc"
  ```
""",

    "MiroMindAI/sd-torchtune": """### Repo Environment: MiroMindAI/sd-torchtune
- Language: Python >=3.9 (use 3.10 for broad compatibility)
- Build system: setuptools (`pyproject.toml`, `[build-system] requires = ["setuptools", "wheel"]`)
- Package manager: pip
- Install:
  ```
  pip install torch torchvision  # install PyTorch first (CPU version for tests)
  pip install -e ".[dev]"        # installs torchtune + all dev/test deps
  pip install torchao            # REQUIRED: tests/conftest.py imports torchao; not in [dev] extras
  ```
- Test runner: pytest 7.4.0 (`pytest tests/ -v`)
  - Existing tests: 159 files under `tests/`
  - Run subset: `pytest tests/torchtune/ -v --without-integration`
- Test deps (from [dev]): pytest==7.4.0, pytest-cov, pytest-mock, pytest-integration, expecttest
- CLI entry: `tune` command (`torchtune._cli.tune:main`)
- Key deps: torchdata, liger-kernel, datasets, huggingface_hub, safetensors, sentencepiece, tiktoken
- Private repo: requires GITHUB_TOKEN (already handled by token injection)
- Recommended base: ubuntu:22.04 + Python 3.10 (avoid heavy CUDA image unless GPU test needed)
""",
}


def get_repo_env_template(repo_name: str) -> str:
    """Return repo-specific env template string, or empty string if not found."""
    return REPO_ENV_TEMPLATES.get(repo_name, "")


def get_system_prompt_dockerfile():
    return SYSTEM_PROMPT_DOCKERFILE



def get_user_prompt_init_dockerfile():
    return USER_PROMPT_INIT_DOCKERFILE

def get_user_prompt_init_dockerfile_using_ubuntu_only():
    return USER_PROMPT_INIT_DOCKERFILE_USE_UBUNTU_ONLY

def get_user_prompt_modify_dockerfile():
    return USER_PROMPT_MODIFY_DOCKERFILE

    
def write_dockerfile_with_retries(
    message_thread: MessageThread,
    output_dir: str,
    task: Task,
    retries=3,
    print_callback: Callable[[dict], None] | None = None,
) -> tuple[str, float, int, int]:
    """
    Since the agent may not always write an applicable patch, we allow for retries.
    This is a wrapper around the actual run.
    """
    new_thread = message_thread
    # new_thread: MessageThread = MessageThread(messages=messages)
    # new_thread = agent_common.replace_system_prompt(new_thread, SYSTEM_PROMPT)
    # # (2) add the initial user prompt
    # user_prompt_init=USER_PROMPT_INIT.format(eval_script_skeleton=eval_script_skeleton)
    # user_prompt_init +="\nFor contents in <original></original> and <patched></patched>, please do not contain them in extra ```\n```. Something like <original>\n```js\n```</original> is forbidden. Keep these contents clean."
    # new_thread.add_user(user_prompt_init)
    # print_acr(user_prompt_init, "dockerfile generation", print_callback=print_callback)

    can_stop = False
    result_msg = ""
    dockerfile_extracted = None
    os.makedirs(output_dir, exist_ok=True)
    for i in range(1, retries + 2):
        if i > 1:
            debug_file = pjoin(output_dir, f"debug_agent_write_dockerfile_{i - 1}.json")
            with open(debug_file, "w") as f:
                json.dump(new_thread.to_msg(), f, indent=4)

        if can_stop or i > retries:
            break

        logger.info(f"Trying to extract a dockerfile. Try {i} of {retries}.")

        raw_dockerfile_file = pjoin(output_dir, f"agent_dockerfile_raw_{i}")

        # actually calling model
        try:
            res_text, *_ = common.SELECTED_MODEL.call(new_thread.to_msg())
        except Exception as e:
            logger.error(f"LLM call failed in dockerfile generation try {i}: {e}")
            continue

        new_thread.add_model(res_text, [])  # no tools

        logger.info(f"Raw dockerfile and produced in try {i}. Writing dockerfile into file.")

        with open(raw_dockerfile_file, "w") as f:
            f.write(res_text)

        print_patch_generation(
            res_text, f"try {i} / {retries}", print_callback=print_callback
        )

        # Attemp to extract a real patch from the raw patch
        # Extract Dockerfile content from model response using regex

        
        # Initialize extraction flags
        dockerfile_extracted = extract_dockerfile_from_response(res_text, output_dir)


        # Determine if both files are extracted
        can_stop = dockerfile_extracted 

        if can_stop:
            result_msg = "Successfully extracted both Dockerfile."
            print_acr(result_msg, f"dockerfile generation try {i}/{retries}", print_callback=print_callback)
            break
        else:
            feedback = "Failed to extract"
            feedback += "Dockerfile" if not dockerfile_extracted else ""
            new_thread.add_user(feedback + ". Please return result in defined format.")
            print_acr(feedback, f"Retry {i}/{retries}", print_callback=print_callback)
    if result_msg == '':
        result_msg = 'Failed to extract'
        
    return result_msg





def extract_dockerfile_from_response(res_text: str, output_dir: str):
        # Process Dockerfile
        dockerfile_path = pjoin(output_dir, "Dockerfile")
        dockerfile_extracted = False
        
        # Pattern 1: <dockerfile> tags
        docker_matches = re.findall(r"<dockerfile>([\s\S]*?)</dockerfile>", res_text)
        for content in docker_matches:
            clean_content = content.strip()
            if clean_content:
                lines = clean_content.splitlines()
                if len(lines) >= 2 and "```" in lines[0] and "```" in lines[-1]:
                    lines = lines[1:-1]
                filtered_content = '\n'.join(lines)
                with open(dockerfile_path, "w") as f:
                    f.write(filtered_content)
                dockerfile_extracted = True
                break  # Stop after first valid extraction

        # Pattern 2: ```dockerfile code block
        if not dockerfile_extracted:
            docker_code_blocks = re.findall(r"```\s*dockerfile\s*([\s\S]*?)```", res_text, re.IGNORECASE)
            for content in docker_code_blocks:
                clean_content = content.strip()
                if clean_content:
                    lines = clean_content.splitlines()
                    if len(lines) >= 2 and "```" in lines[0] and "```" in lines[-1]:
                        lines = lines[1:-1]
                    filtered_content = '\n'.join(lines)
                    with open(dockerfile_path, "w") as f:
                        f.write(filtered_content)
                    dockerfile_extracted = True
                    break
        return dockerfile_extracted

