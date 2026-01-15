"""Modular transfer agent implementation."""

from __future__ import annotations

import ast
import difflib
import io
import json
import logging
import os
import re
import shutil
import tarfile
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.exceptions import ChunkedEncodingError, ConnectionError as RequestsConnectionError
from docker import from_env
from docker.errors import APIError, BuildError

from .utils.errors import (
    CommandError,
    EvalNoExitCodeError,
    EvalTimeoutError,
    ImageBuildError,
    ParsingError,
)
from .runtime import DockerRuntime

from .checker import ContainerChecker, DEFAULT_TOOL_NAMES
from .utils.iteration import IterationRecorder
from .utils.logging_utils import utc_now
from .utils.prompts import (
    HEREDOC_DELIMITER,
    get_dockerfile_selfcheck_prompt,
    get_eval_review_prompt,
    get_system_prompt,
    get_unroot_prompt,
)
from .utils.responses import ResponseTracker

LOGGER = logging.getLogger(__name__)

def robust_clean_text(text: str) -> str:
    if len(text) >= 2 and (
        (text.startswith('"') and text.endswith('"'))
        or (text.startswith("'") and text.endswith("'"))
    ):
        inner = text[1:-1]
        try:
            return bytes(inner, "utf-8").decode("unicode_escape")
        except Exception:
            return inner
    return text


def docker_image_name(instance_id: str) -> str:
    safe = instance_id.replace("/", "-").replace(":", "-").lower()
    return f"{safe}_swefactory_root"


class TransferAgent:
    """Refactored transfer agent with modular components."""

    def __init__(
        self,
        task_dict: Dict[str, Any],
        max_iteration_num: int,
        output_path: str,
        eval_timeout: int = 300,
    ) -> None:
        self.task_dict = task_dict
        self.max_iteration_num = max_iteration_num
        self.output_dir = Path(output_path)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.original_dockerfile: str = task_dict["dockerfile"]
        self.original_eval_script: str = task_dict["eval_script"]
        self.repo_placeholder: str = task_dict.get("repo_placeholder", "")
        if not self.repo_placeholder:
            repo_name = task_dict.get("repo")
            if not repo_name:
                raise ValueError("task_dict must include repo or repo_placeholder")
            self.repo_placeholder = repo_name.replace("/", "__")
        self.repo_copy_path: str = task_dict.get("repo_copy_path", "")
        self.test_patch: str = ""
        self.original_eval_script_skeleton: str = ""
        self._extract_test_patch()

        self.agent_context: List[Dict[str, str]] = []
        self.system_prompt = get_system_prompt()
        self.cost = 0.0
        self.input_tokens = 0
        self.output_tokens = 0

        self.responses = ResponseTracker(self.output_dir)
        self.iter_recorder = IterationRecorder(self.output_dir)
        self.self_check_max_revisions = int(os.environ.get("SELF_CHECK_MAX_REVISIONS", "2"))

        self.manual_dockerfile_path = None
        self.execution_mode = "auto"


        self.tool_names: list[str] = []

        self._artifacts_generated = False
        self._dockerfile_modified = False
        self._build_context_entries: List[Dict[str, Any]] = []

        self.name = docker_image_name(task_dict["instance_id"])
        self.docker_runtime: Optional[DockerRuntime] = None
        self.image_build_success = False
        self.image_correct = False
        self.client = from_env()
        self.logger = LOGGER

        self.last_exec_exit_code: Optional[int] = None
        self.last_exec_output: Optional[str] = None
        self.current_dockerfile_text = self.original_dockerfile
        self.current_eval_script_text = self.original_eval_script
        self.current_notes: Optional[str] = None
        self.eval_timeout = eval_timeout

    # ------------------------------------------------------------------
    # Prompt / context helpers
    # ------------------------------------------------------------------
    def _init_context(self) -> None:
        self._add_context(self.system_prompt, "system")

    def _copy_tools_to_container(self, checker: ContainerChecker) -> Dict[str, str]:
        # 工具拷贝逻辑已停用；保留空占位以兼容调用方。
        return {}

    def _add_context(self, message: str, role: str) -> None:
        self.agent_context.append({"role": role, "content": message})

    def _extract_test_patch(self) -> None:
        pattern = re.compile(
            rf"git apply -v - <<'{HEREDOC_DELIMITER}'\n(.*?)\n{HEREDOC_DELIMITER}",
            re.DOTALL,
        )
        match = pattern.search(self.original_eval_script)
        if match:
            self.test_patch = match.group(1)
            self.original_eval_script_skeleton = self.original_eval_script.replace(
                self.test_patch, "[CONTENT_OF_TEST_PATCH]"
            )
        else:
            self.test_patch = ""
            self.original_eval_script_skeleton = self.original_eval_script

    def _prepare_build_context(
        self,
        dockerfile_text: str,
        run_tests_path: Path,
    ) -> Tuple[str, List[Dict[str, Any]]]:
   
        return dockerfile_text, []

    # ------------------------------------------------------------------
    # Model interaction
    # ------------------------------------------------------------------
    def _call_model(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ParsingError("Missing OPENAI_API_KEY for LLM calls")
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        endpoint = f"{base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        max_attempts = 3
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.post(endpoint, headers=headers, json=payload, timeout=60)
            except (ChunkedEncodingError, RequestsConnectionError) as exc:
                self.logger.warning(
                    "LLM Bad Request（attempt %d/%d）: %s",
                    attempt,
                    max_attempts,
                    exc,
                )
                last_exc = exc
                if attempt == max_attempts:
                    raise ParsingError("LLM request failed after multiple retries") from exc
                continue

            if response.status_code != 200:
           
                raise Exception(f"API Calling fails: {response.status_code} - {response.text}")

            try:
                data = response.json()
            except json.JSONDecodeError as exc:  # noqa: PERF203
                body_preview = response.text[:500].replace("\n", " ")
                self.logger.warning(
                    "LLM JSON decode失败（attempt %d/%d）: %s | preview=%s",
                    attempt,
                    max_attempts,
                    exc,
                    body_preview,
                )
                last_exc = exc
                if attempt == max_attempts:
                    raise ParsingError(
                        "LLM response JSON decode failed after multiple retries"
                    ) from exc
                continue

            self.responses.log_call(payload, data)
            stats = self.responses.get_stats(self.responses.current_iteration)
            self.cost = self.responses.total_cost
            self.input_tokens = self.responses.total_input_tokens
            self.output_tokens = self.responses.total_output_tokens
            return data

        if last_exc is not None:
            raise ParsingError("LLM response could not be parsed") from last_exc
        raise ParsingError("LLM response could not be obtained")

    def _call_model_messages(self) -> Tuple[str, Dict[str, Any], float]:
        max_attempts = 3
        model_name = os.getenv("OPENAI_MODEL")
        if not model_name:
            raise ParsingError("Missing OPENAI_MODEL for LLM calls")
        for attempt in range(1, max_attempts + 1):
            payload = {"model": model_name, "messages": self.agent_context}
            data = self._call_model(payload)
            choice = data["choices"][0]["message"]
            content = choice.get("content", "")
            usage = data.get("usage", {})
            cost = float(usage.get("cost", 0.0) or 0.0)

            if content and content.strip():
                self._add_context(content, "assistant")
                return content, usage, cost

            self.logger.warning(
                "LLM returned empty content (attempt %d/%d); retrying", attempt, max_attempts
            )

        raise ParsingError("LLM response contained no content after multiple retries")

    # ------------------------------------------------------------------
    # Artifact generation helpers
    # ------------------------------------------------------------------
    def extract_and_save(self, response: str, field_name: str, filename: str) -> str:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.S)
        raw = match.group(1) if match else response
        normalized = raw.replace("\r\n", "\n").replace("\r", "\n").strip()

        try:
            try:
                parsed = json.loads(normalized)
            except json.JSONDecodeError:
                parsed = ast.literal_eval(normalized)

            value = parsed.get(field_name)
            if value in (None, "<None>"):
                raise KeyError(field_name)
            content = robust_clean_text(value)
        except Exception as exc:
            original = getattr(self, f"original_{field_name}")
            print(f"[Warning] extract_and_save for '{field_name}' failed: {exc}. Using original.")
            content = original

        out_path = self.output_dir / filename
        out_path.write_text(content, encoding="utf-8")
        print(f"Wrote file: {out_path}")
        return content

    def _reset_generated_artifacts(self) -> None:
        self._artifacts_generated = False

    def _diff_text(self, original: str, modified: str, from_label: str, to_label: str) -> str:
        diff = difflib.unified_diff(
            original.splitlines(),
            modified.splitlines(),
            fromfile=from_label,
            tofile=to_label,
            lineterm="",
        )
        return "\n".join(diff)

    def _dockerfile_checklist_text(self) -> str:
        items = [
            "1) Repo lives under `/testbed` – keep `WORKDIR /testbed` and ensure the clone/checkout ends up directly in `/testbed` (not nested). Preserve original post-clone commands (checkout, submodules, chmod).",
            "2) Environment usable – keep the baseline env/location/name (whether under /opt or /testbed); do not relocate or duplicate it. Ensure `bash -lc` immediately picks up the env via `/root/.profile` (e.g., `source /testbed/venv/bin/activate` or matching conda/Poetry/pdm). ENV PATH is optional if the profile activation already works; `.bashrc` is optional. One repo, one environment; do not create extra venvs/conda envs under new paths.",
            "3) Guard the baseline – base image, Python/toolchain versions, and other global configuration should match the original environment unless a requirement forces a tweak. If you touched them temporarily, restore the original values before you’re done. Do not add new post-clone steps (submodule update, chmod, cleanup) that were not in the original.",
            "4) Leave a tidy repo – the patch snapshot stages **all** changes (`git add -A`), so any build artefacts under `/testbed` that are not part of the task should be excluded via `/testbed/.git/info/exclude` (env dirs, caches, and lockfiles generated during build like `pdm.lock` are all OK to exclude). Do not exclude anything outside `/testbed` (especially `/opt`) and do not exclude the whole repo.",
        ]
        return "\n".join(items)

    def _self_review_dockerfile(self, candidate: str, diff_text: str) -> Tuple[bool, str]:
        checklist = self._dockerfile_checklist_text()
        rendered_diff = diff_text if diff_text.strip() else "(no diff)"
        prompt = get_dockerfile_selfcheck_prompt(
            self.original_dockerfile,
            candidate,
            rendered_diff,
            checklist,
        )
        max_attempts = 2

        for attempt in range(max_attempts):
            payload = {
                "model": os.getenv("OPENAI_MODEL"),
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": prompt},
                ],
            }
            data = self._call_model(payload)
            message = data["choices"][0]["message"]["content"]
            try:
                parsed = self._parse_json_response(message)
            except Exception as exc:
                if attempt + 1 == max_attempts:
                    raise ParsingError("Self-check response was not valid JSON") from exc
                continue

            needs_revision = bool(parsed.get("needs_revision", False))
            feedback = parsed.get("feedback", "") or ""
            feedback = robust_clean_text(str(feedback))
            return needs_revision, feedback

        raise ParsingError("Self-check did not return valid JSON")

    def _review_eval_script(
        self,
        final_dockerfile: str,
        docker_diff: str,
    ) -> Tuple[str, bool, Optional[str]]:
        """Phase 3: ask the model if eval/run_tests.sh needs changes."""
        placeholder = "[CONTENT_OF_TEST_PATCH]"
        prompt = get_eval_review_prompt(final_dockerfile, docker_diff, self.original_eval_script_skeleton)
        max_attempts = 2
        notes: Optional[str] = None

        for attempt in range(max_attempts):
            payload = {
                "model": os.getenv("OPENAI_MODEL"),
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": prompt},
                ],
            }
            data = self._call_model(payload)
            message = data["choices"][0]["message"]["content"]
            try:
                parsed = self._parse_json_response(message)
            except Exception as exc:
                if attempt + 1 == max_attempts:
                    raise ParsingError("Eval review response was not valid JSON") from exc
                continue

            raw_notes = parsed.get("notes")
            if raw_notes and str(raw_notes).strip() not in {"", "<None>"}:
                notes = robust_clean_text(str(raw_notes))

            script_value = parsed.get("eval_script")
            if script_value is None or str(script_value).strip() == "<None>":
                return self.original_eval_script, False, notes

            script_text = robust_clean_text(str(script_value))
            if placeholder not in script_text:
                # If the model failed to preserve the placeholder, fall back to the skeleton to avoid corrupting the patch.
                script_text = self.original_eval_script_skeleton
            script_text = script_text.replace(placeholder, self.test_patch)
            return script_text, True, notes

        raise ParsingError("Eval review did not return valid JSON")

    def _append_failure_feedback(
        self,
        docker_diff: Optional[str],
        eval_diff: Optional[str],
        error_message: str,
    ) -> None:
        parts = [
            "We ran the revised environment and it failed.",
            "Purpose: execute /opt/run_tests.sh to verify the repository patch.",
            "Command: bash -lc 'bash /opt/run_tests.sh'",
            "Observed error log:",
            f"```\n{error_message}\n```",
        ]
        if docker_diff:
            parts.append("Current Dockerfile diff vs. original:")
            parts.append(f"```diff\n{docker_diff}\n```")
        if eval_diff:
            parts.append("Current run_tests.sh diff vs. original:")
            parts.append(f"```diff\n{eval_diff}\n```")
        parts.append("Please diagnose the failure and update the Dockerfile/run_tests.sh accordingly.")
        self._add_context("\n".join(parts), "user")

    @staticmethod
    def _is_self_check_failure(exc: CommandError) -> bool:
        details = getattr(exc, "failures", None) or []
        return any(detail.get("cmd") == "self_check" for detail in details)

    def _format_selfcheck_feedback(self, feedback: str) -> str:
        feedback = feedback.strip() or "Checklist reported unmet requirements."
        return (
            "A reviewer agent re-checked your Dockerfile and flagged the following checklist violations."
            "\nTreat each line below as a TODO list—fix every item while keeping unrelated lines untouched."
            f"\n\nTODOs:\n{feedback}\n"\
            "After applying the fixes, respond with the usual JSON payload describing the revised Dockerfile."
        )

    @staticmethod
    def _summarize_diff_paths(diff_output: str, max_paths: int = 50) -> str:
        """Extract touched file paths from a unified diff for concise reporting."""
        paths: list[str] = []
        for line in diff_output.splitlines():
            if line.startswith("diff --git "):
                parts = line.split()
                if len(parts) >= 4:
                    path = parts[2].removeprefix("a/")
                    if path not in paths:
                        paths.append(path)
                        if len(paths) >= max_paths:
                            break
        if not paths:
            return "(no file paths detected)"
        extra = ""
        if len(paths) == max_paths:
            extra = " ... (truncated)"
        return "\n".join(paths) + extra

    @staticmethod
    def _omit_diff_body(diff_output: str) -> str:
        """Replace diff hunks with a placeholder to keep feedback short."""
        lines: list[str] = []
        in_hunk = False
        for line in diff_output.splitlines():
            if line.startswith("@@"):
                if not in_hunk:
                    lines.append("[HUNK CONTENT OMITTED]")
                    in_hunk = True
                continue
            if line.startswith("diff --git "):
                in_hunk = False
                lines.append(line)
            elif not in_hunk:
                lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _trim_output(text: str, max_lines: int = 80, max_chars: int = 4000) -> str:
        snippet = text.strip()
        if not snippet:
            return "(command produced no output)"
        truncated = False
        if len(snippet) > max_chars:
            snippet = snippet[:max_chars]
            truncated = True
        lines = snippet.splitlines()
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            truncated = True
        preview = "\n".join(lines)
        if truncated:
            preview += "\n... (truncated)"
        return preview

    def _format_git_diff_failure(
        self,
        diff_output: str,
        diff_length: int,
        max_patch_chars: int,
    ) -> str:
        path_summary = self._summarize_diff_paths(diff_output)
        preview = self._omit_diff_body(diff_output)
        preview = self._trim_output(preview)
        return (
            "Patch hygiene check failed before we re-ran the tests.\n"
            "Motivation: we snapshot `git add -A && git diff --cached` to ensure the generated patch stays reviewable and doesn't include chmod or venv noise.\n"
            f"Command output contained {diff_length} characters, which exceeds the {max_patch_chars}-character budget.\n"
            "Please tighten the Dockerfile so `/testbed` remains clean—prefer adding precise paths under `/testbed/.git/info/exclude` for env/cache artefacts, instead of moving files.\n"
            "Files involved (from diff headers):\n"
            f"{path_summary}\n"
            "Tip: writing `/testbed/.git/info/exclude` is normal; include the exact file paths shown above to keep the patch snapshot clean.\n"
            "Example (edit the path list to match the files above):\n"
            "```bash\n"
            "cd /testbed && mkdir -p .git/info\n"
            "printf '.pdm-python\\npdm.lock\\n__pycache__/\\n.pytest_cache/\\n' >> .git/info/exclude\n"
            "```\n"
            "Review the preview below, trim the noise, and try again:\n"
            f"```diff\n{preview}\n```"
        )

    def _format_git_diff_command_failure(self, exit_code: int, output: str) -> str:
        preview = self._trim_output(output, max_lines=80)
        return (
            "Patch preview command failed before tests.\n"
            "Motivation: we run `git add -A && git diff --cached` to capture the exact patch we will ship.\n"
            f"Command exit code: {exit_code}.\n"
            "Please fix the underlying git error (e.g., conflicts, missing files) and rerun once the diff succeeds.\n"
            "Command output preview:\n"
            f"```\n{preview}\n```"
        )

    def _format_selfcheck_limit_failure(self, attempts: List[Dict[str, Any]]) -> str:
        latest_feedback = attempts[-1]["feedback"] if attempts else "(no reviewer notes captured)"
        return (
            "We tried to revise the Dockerfile multiple times inside this iteration but the self-check still failed.\n"
            f"Each iteration allows up to {self.self_check_max_revisions} inline regeneration(s); please address the reviewer notes below"
            " before attempting again.\n\nLatest reviewer notes:\n"
            f"{latest_feedback}"
        )


    def _parse_json_response(self, response: str) -> Dict[str, Any]:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.S)
        raw = match.group(1) if match else response
        normalized = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
        try:
            return json.loads(normalized)
        except json.JSONDecodeError:
            try:
                return ast.literal_eval(normalized)
            except Exception:
                raise ValueError("Failed to parse JSON response from model")

    def _generate_artifacts_once(self, *, force: bool = False) -> None:
        if self._artifacts_generated and not force:
            return

        max_attempts = 3
        last_exc: Optional[Exception] = None

        for attempt in range(max_attempts):
            if attempt > 0:
                self._add_context(
                    (
                        "We could not parse your previous answer into JSON. Please respond exactly with "
                        '{"dockerfile": "... or <None>", "eval_script": "... or <None>", "notes": "..."}.'
                    ),
                    "user",
                )

            response, *_ = self._call_model_messages()

            try:
                parsed = self._parse_json_response(response)
            except Exception as exc:
                last_exc = exc
                self.logger.warning("LLM JSON parse failed (attempt %d/%d): %s", attempt + 1, max_attempts, exc)
                continue

            docker_value = parsed.get("dockerfile")
            notes_value = parsed.get("notes")

            if docker_value and str(docker_value).strip() != "<None>":
                cleaned = robust_clean_text(str(docker_value))
                self.current_dockerfile_text = cleaned
                self._dockerfile_modified = cleaned.strip() != self.original_dockerfile.strip()
            else:
                self._dockerfile_modified = self.current_dockerfile_text.strip() != self.original_dockerfile.strip()

            if notes_value and str(notes_value).strip() not in {"", "<None>"}:
                self.current_notes = robust_clean_text(str(notes_value))
            else:
                self.current_notes = None

            self._artifacts_generated = True
            return

        raise ParsingError("Model response was not valid JSON") from last_exc



    # ------------------------------------------------------------------
    # Docker helpers
    # ------------------------------------------------------------------
    def dockerfile_check(self, dockerfile_str: str) -> bool:
        prompt = (
            "Please analyze whether the following Dockerfile correctly installs the repository into the `/testbed` directory (and not elsewhere).\n"
            "You should look for patterns such as:\n\n"
            "# Clone the target repository into `/testbed`, for example:\n"
            "# RUN git clone <repository_url> /testbed && \\\n"
            "\n"
            "Implementations may vary—if there are equivalent operations that result in the repo being placed under `/testbed`, consider it True.\n"
            "Explain your reasoning briefly and then return only \"True\" or \"False\".\n\n"
            f"Dockerfile content:\n{dockerfile_str}\n"
        )
        payload = {"model": os.getenv("OPENAI_MODEL"), "messages": [{"role": "user", "content": prompt}]}
        data = self._call_model(payload)
        ans = data["choices"][0]["message"]["content"].strip()
        if "True" in ans:
            return True
        if "False" in ans:
            return False
        raise ImageBuildError(f"LLM confused. Unexpected LLM response: {ans!r}")

    def build_image(self, dockerfile_text: str, iteration_dir: Path) -> None:
        client = self.client
        dockerfile_bytes = dockerfile_text.encode("utf-8")
        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w") as tar:
            info = tarfile.TarInfo(name="Dockerfile")
            info.size = len(dockerfile_bytes)
            tar.addfile(info, io.BytesIO(dockerfile_bytes))

            added: set[str] = set()
            for entry in self._build_context_entries:
                source = Path(entry["source"])
                arcname = entry["arcname"].rstrip("/")
                if arcname in added:
                    continue
                if not source.exists():
                    raise FileNotFoundError(f"Build context source missing: {source}")
                tar.add(str(source), arcname=arcname)
                added.add(arcname)
        tar_stream.seek(0)

        print(f"[BUILD] Starting Docker build for image tag: {self.name}")
        try:
            image, raw_logs = client.images.build(
                fileobj=tar_stream,
                custom_context=True,
                dockerfile="Dockerfile",
                tag=self.name,
                rm=True,
            )
            logs = list(raw_logs)
            build_log = iteration_dir / f"{self.name}_build.log"
            with build_log.open("w", encoding="utf-8") as fh:
                for chunk in logs:
                    if "stream" in chunk:
                        fh.write(chunk["stream"])
            print(f"[BUILD] Build logs written to: {build_log}")

            print("[BUILD OUTPUT]")
            for chunk in logs:
                if "stream" in chunk:
                    line = re.sub(r"\x1b\[[0-9;]*m", "", chunk["stream"])
                    print(line, end="")
        except BuildError as exc:
            print(f"[BUILD][ERROR] BuildError: {exc}")
            raise ImageBuildError(f"BuildError: {exc}")
        except APIError as exc:
            print(f"[BUILD][ERROR] APIError: {exc}")
            raise ImageBuildError(f"APIError: {exc}")

    def start_container(self, iteration: int):
        iter_name = f"{self.name}_{iteration}"
        self.docker_runtime = DockerRuntime(
            image=self.name,
            name=iter_name,
            command=["/bin/bash", "-l"],
            **getattr(self, "docker_kwargs", {}),
        )
        return self.docker_runtime.container

    # ------------------------------------------------------------------
    # Debug helpers
    # ------------------------------------------------------------------
    def _debug_dump(self, base: Path, why: str, error: Optional[Exception] = None) -> None:
        base.mkdir(parents=True, exist_ok=True)
        trace_path = base / "error.trace"
        with trace_path.open("w", encoding="utf-8") as fh:
            fh.write(f"Reason: {why}\n")
            if error:
                traceback.print_exc(file=fh)

        ctx_path = base / "agent_context.json"
        ctx_path.write_text(json.dumps(self.agent_context, indent=2, ensure_ascii=False), encoding="utf-8")

        for name in ("Dockerfile", "run_tests.sh", "eval_script.sh"):
            src = base.parent / name
            dst = base / f"{name}.debug"
            if src.exists():
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    # ------------------------------------------------------------------
    # Main execution loop
    # ------------------------------------------------------------------
    def run_task(self) -> bool:
        (self.output_dir / "Dockerfile_original").write_text(self.original_dockerfile, encoding="utf-8")
        (self.output_dir / "run_tests_original.sh").write_text(self.original_eval_script, encoding="utf-8")
        (self.output_dir / "eval_script_original.sh").write_text(self.original_eval_script, encoding="utf-8")

        status = "failed"
        final_iteration: Optional[int] = None
        reason: Optional[str] = None
        last_iteration_attempted: Optional[int] = None
        exit_reason: Optional[str] = None
        abort_due_to_timeout = False

        self.agent_context = []
        self._init_context()
        self._add_context(
            get_unroot_prompt(self.original_dockerfile, self.original_eval_script_skeleton),
            "user",
        )

        for iteration in range(self.max_iteration_num):
            last_iteration_attempted = iteration
            self.responses.set_iteration(iteration)
            ctx = self.iter_recorder.start(iteration)
            iter_dir = ctx.directory
            checker: Optional[ContainerChecker] = None
            iteration_success = False
            error_payload: Optional[Dict[str, Any]] = None
            last_exc: Optional[Exception] = None
            dockerfile_placeholder_path: Optional[Path] = None

            self._reset_generated_artifacts()

            docker_diff_text: Optional[str] = None
            eval_diff_text: Optional[str] = None

            try:
                phase1_attempts: List[Dict[str, Any]] = []
                selfcheck_attempts: List[Dict[str, Any]] = []
                dockerfile_text_raw = ""
                docker_diff_text = ""
                dockerfile_validated = False

                phase1_attempts: List[Dict[str, Any]] = []
                selfcheck_attempts: List[Dict[str, Any]] = []
                dockerfile_text_raw = ""
                docker_diff_text = ""
                dockerfile_validated = False

                for revision in range(1, self.self_check_max_revisions + 1):
                    if revision > 1:
                        self._reset_generated_artifacts()
                    self._generate_artifacts_once(force=True)
                    self._build_context_entries = []
                    dockerfile_candidate = self.current_dockerfile_text
                    diff_text = self._diff_text(
                        self.original_dockerfile,
                        dockerfile_candidate,
                        "Dockerfile (original)",
                        "Dockerfile (candidate)",
                    )
                    phase1_attempts.append(
                        {
                            "attempt": revision,
                            "notes": self.current_notes,
                            "dockerfile_modified": self.current_dockerfile_text.strip()
                            != self.original_dockerfile.strip(),
                        }
                    )

                    needs_revision, review_feedback = self._self_review_dockerfile(
                        dockerfile_candidate,
                        diff_text,
                    )
                    selfcheck_attempts.append(
                        {
                            "attempt": revision,
                            "needs_revision": needs_revision,
                            "feedback": review_feedback,
                        }
                    )

                    if not needs_revision:
                        dockerfile_validated = True
                        dockerfile_text_raw = dockerfile_candidate
                        docker_diff_text = diff_text
                        break

                    feedback_prompt = self._format_selfcheck_feedback(review_feedback)
                    self._add_context(feedback_prompt, "user")

                ctx.metadata["phase1"] = {
                    "attempts": phase1_attempts,
                    "final_notes": self.current_notes,
                    "dockerfile_modified": bool(phase1_attempts and phase1_attempts[-1]["dockerfile_modified"]),
                }
                ctx.metadata["phase2_selfcheck"] = {
                    "attempts": selfcheck_attempts,
                    "needs_revision": not dockerfile_validated,
                    "feedback": selfcheck_attempts[-1]["feedback"] if selfcheck_attempts else "",
                    "revision_applied": dockerfile_validated,
                }

                if not dockerfile_validated:
                    msg = self._format_selfcheck_limit_failure(selfcheck_attempts)
                    raise CommandError(
                        [
                            {
                                "cmd": "self_check",
                                "exit_code": 1,
                                "user_message": msg,
                            }
                        ]
                    )

                dockerfile_text_raw = dockerfile_candidate

                # --- Phase 3: 评估脚本复核 ---
                rendered_diff = docker_diff_text if docker_diff_text.strip() else "(no diff)"
                eval_script_text, eval_changed, eval_notes = self._review_eval_script(
                    dockerfile_candidate,
                    rendered_diff,
                )
                self.current_eval_script_text = eval_script_text
                eval_diff_text = self._diff_text(
                    self.original_eval_script,
                    eval_script_text,
                    "eval.sh (original)",
                    "eval.sh (candidate)",
                )
                ctx.metadata["phase3_eval"] = {
                    "changed": eval_changed,
                    "notes": eval_notes,
                }

                run_tests_path = iter_dir / "run_tests.sh"
                run_tests_path.write_text(self.current_eval_script_text, encoding="utf-8")
                ctx.metadata["run_tests_path"] = self.iter_recorder.relative(run_tests_path)
                ctx.metadata["eval_script_path"] = self.iter_recorder.relative(run_tests_path)

                dockerfile_placeholder_path = iter_dir / "Dockerfile.placeholders"
                dockerfile_placeholder_path.write_text(dockerfile_text_raw, encoding="utf-8")
                ctx.metadata["dockerfile_placeholder_path"] = self.iter_recorder.relative(
                    dockerfile_placeholder_path
                )

                processed_dockerfile_text, context_entries = self._prepare_build_context(
                    dockerfile_text_raw,
                    run_tests_path,
                )
                self._build_context_entries = context_entries

                dockerfile_path = iter_dir / "Dockerfile"
                dockerfile_path.write_text(processed_dockerfile_text, encoding="utf-8")
                ctx.metadata["dockerfile_path"] = self.iter_recorder.relative(dockerfile_path)

                ctx.metadata["dockerfile_normalized"] = self._dockerfile_modified
                ctx.metadata["build_context_entries"] = [entry["arcname"] for entry in context_entries]

                # --- Phase 4: 构建镜像并执行真实校验 ---
                checker = ContainerChecker(
                    workdir="/testbed",
                    log_file=str(iter_dir / f"{self.name}_iter_{iteration}_log.log"),
                    tools_to_check=None,
                    gold_patch=self.task_dict.get("patch"),
                )

                self.build_image(processed_dockerfile_text, iteration_dir=iter_dir)
                build_log = iter_dir / f"{self.name}_build.log"
                if build_log.exists():
                    ctx.metadata["build_log_path"] = self.iter_recorder.relative(build_log)
                self.image_build_success = True

                container = self.start_container(iteration)
                checker.set_container(container)
                checker.set_runtime(self.docker_runtime)

           
                ctx.metadata["command_checks"] = []

                ctx.metadata["checklist"] = {}

                diff_cmd = "git add -A && git diff --cached"
                diff_code, diff_output = checker.run_cmd(
                    diff_cmd,
                    timeout=180,
                )
                diff_length = len(diff_output)
                ctx.metadata["checklist"]["git_diff_preview"] = {
                    "exit_code": diff_code,
                    "length": diff_length,
                    "diff_output":diff_output
                }
                if diff_code != 0:
                    msg = self._format_git_diff_command_failure(diff_code, diff_output)
                    raise CommandError([
                        {
                            "cmd": diff_cmd,
                            "exit_code": diff_code,
                            "user_message": msg,
                        }
                    ])
                max_patch_chars = int(os.environ.get("MAX_PATCH_CHARS", "50000"))
                if diff_length > max_patch_chars:
                    msg = self._format_git_diff_failure(diff_output, diff_length, max_patch_chars)
                    raise CommandError([
                        {
                            "cmd": diff_cmd,
                            "exit_code": 0,
                            "user_message": msg,
                        }
                    ])

                # --- 8) Final evaluation run ---
                eval_result = checker.run_eval_script(
                    local_script=run_tests_path,
                    iteration_dir=iter_dir,
                    log_name=f"{self.name}_{iteration}_exec.log",
                    dest_path="/opt/run_tests.sh",
                    timeout=self.eval_timeout,
                    copy_from_host=True,
                )
                self.last_exec_exit_code = eval_result["exit_code"]
                self.last_exec_output = eval_result["output"]
                ctx.metadata["exec_log_path"] = self.iter_recorder.relative(eval_result["log_path"])
                ctx.metadata["eval_result"] = {
                    "success": True,
                    "exit_code": eval_result["exit_code"],
                    "output_sample": eval_result["output"][:1000],
                }

                self.image_correct = True

                checker.record_eval((True, eval_result["output"]))
                summary = checker.summary()
                summary_path = iter_dir / "summary.json"
                summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
                ctx.metadata["checker_summary_path"] = self.iter_recorder.relative(summary_path)

                # Persist final artifacts at root level for downstream use
                resolved_dockerfile_text = dockerfile_text_raw

                # Save the generated Dockerfile (placeholders no longer used)
                placeholder_out = self.output_dir / "Dockerfile"
                placeholder_out.write_text(dockerfile_text_raw, encoding="utf-8")
                (self.output_dir / "Dockerfile.resolved").write_text(
                    resolved_dockerfile_text, encoding="utf-8"
                )
                shutil.copy2(run_tests_path, self.output_dir / "run_tests.sh")
                shutil.copy2(run_tests_path, self.output_dir / "eval_script.sh")

                root_summary = self.output_dir / "summary.json"
                if not root_summary.exists():
                    root_summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

                iteration_success = True
                status = "success"
                final_iteration = iteration

            except (ImageBuildError, CommandError, EvalNoExitCodeError, EvalTimeoutError) as exc:
                last_exc = exc
                reason = str(exc)
                error_payload = {
                    "type": exc.__class__.__name__,
                    "message": str(exc),
                }
                if isinstance(exc, CommandError):
                    error_payload["details"] = exc.failures
                is_self_check = isinstance(exc, CommandError) and self._is_self_check_failure(exc)
                if not is_self_check:
                    self._handle_error_context(exc)
                    self._append_failure_feedback(docker_diff_text, eval_diff_text, str(exc))
                self._reset_generated_artifacts()
                timeout_hit, timeout_label = self._is_timeout_error(exc)
                if timeout_hit:
                    abort_due_to_timeout = True
                    exit_reason = "timeout"
                    reason = timeout_label or reason
                    status = "failed"

            except Exception as exc:  # pragma: no cover - safeguard for unknown errors
                last_exc = exc
                reason = str(exc)
                error_payload = {"type": exc.__class__.__name__, "message": str(exc)}
                if not isinstance(exc, ParsingError):
                    self._handle_error_context(exc, message=f"Unexpected error occurred: {exc}.")
                self._debug_dump(iter_dir, "fatal exception", exc)
                if not isinstance(exc, ParsingError):
                    self._append_failure_feedback(docker_diff_text, eval_diff_text, str(exc))
                self._reset_generated_artifacts()
                timeout_hit, timeout_label = self._is_timeout_error(exc)
                if timeout_hit:
                    abort_due_to_timeout = True
                    exit_reason = "timeout"
                    reason = timeout_label or reason
                    status = "failed"

            finally:
                stats = self.responses.get_stats(iteration)
                self.iter_recorder.finalize(ctx, stats, iteration_success, error_payload)

                if checker:
                    try:
                        checker.dump_state(iter_dir)
                    except Exception as dump_exc:
                        self.logger.warning("dump_state failed: %s", dump_exc)

                if last_exc and not iteration_success:
                    self._debug_dump(iter_dir, f"iteration {iteration} error", last_exc)

                if self.docker_runtime:
                    try:
                        self.docker_runtime.stop()
                    except Exception:
                        pass
                    self.docker_runtime = None

                if not (self.image_build_success and self.image_correct):
                    try:
                        self.client.images.remove(self.name, force=True)
                        self.image_build_success = False
                    except Exception:
                        pass

                if not iteration_success:
                    self.image_correct = False

                self.responses.set_iteration(None)

            if iteration_success:
                break
            if abort_due_to_timeout:
                break

        if status != "success":
            next_index = (last_iteration_attempted or -1) + 1
            final_dir = self.output_dir / f"iteration_{next_index}"
            self._debug_dump(final_dir, "exceeded max iterations")
            if reason is None:
                reason = f"Exceeded max iterations ({self.max_iteration_num})"

        self._write_status(status, final_iteration, reason, last_iteration_attempted, exit_reason=exit_reason)
        return status == "success"

    # ------------------------------------------------------------------
    # Helper methods for error messaging and status
    # ------------------------------------------------------------------
    def _handle_error_context(self, exc: Exception, message: Optional[str] = None) -> None:
        if message is None:
            if isinstance(exc, ImageBuildError):
                message = (
                    "We were building the image so we could run your tests, but the Docker build step failed."
                    " That stage simply replays your Dockerfile, so a failure almost always means the new edits"
                    " broke the build context (COPY paths, RUN ordering, or missing assets). Please re-check"
                    " that the repo lands in /testbed, original RUN commands remain intact, and any required"
                    " files (run_tests.sh, env setup) are present."
                    f"\nRaw Docker error: {exc}"
                )
            elif isinstance(exc, CommandError):
                eval_failure = None
                for failure in getattr(exc, "failures", []) or []:
                    cmd = failure.get("cmd") or ""
                    if cmd.endswith("run_tests.sh") or "/run_tests.sh" in cmd:
                        eval_failure = failure
                        break

                if eval_failure:
                    output = eval_failure.get("output") or ""
                    tail_lines = output.strip().splitlines()[-100:]
                    tail_text = "\n".join(tail_lines)
                    exit_code = eval_failure.get("exit_code")
                    message = (
                        "We reached the final `bash /opt/run_tests.sh` stage and it exited non-zero."
                        " That command is the exact verification script from the dataset; if it fails, the"
                        " patch is still broken inside your environment. Please study the tail of the log"
                        " below, reproduce the failure inside the container, and adjust the Dockerfile or"
                        " run_tests.sh so the suite passes."
                        f"\nExit code: {exit_code}"
                    )
                    if tail_text:
                        message += (
                            "\nCaptured tail of /opt/run_tests.sh output:\n"
                            f"```\n{tail_text}\n```"
                        )
                else:
                    message = None
            elif isinstance(exc, EvalNoExitCodeError):
                message = (
                    "We ran `/opt/run_tests.sh` to check the patch, but the script never printed"
                    " `OMNIGRIL_EXIT_CODE=…`, so we cannot tell whether the run passed. This usually means the"
                    " script exited early, changed the logging contract, or forgot to echo the sentinel."
                    " always ends by"
                    " printing `OMNIGRIL_EXIT_CODE=<status>`. ."
                )
        if message:
            self._add_context(message, "user")

    @staticmethod
    def _is_timeout_error(exc: Exception) -> Tuple[bool, str]:
        """Detect whether an exception was caused by an execution timeout."""
        timeout_marker = "The command took too long to execute"
        if isinstance(exc, EvalTimeoutError):
            return True, f"timeout: eval_script exceeded {exc.timeout}s"
        if isinstance(exc, CommandError):
            for failure in getattr(exc, "failures", []) or []:
                output = failure.get("output") or failure.get("user_message") or ""
                if timeout_marker in output:
                    return True, f"timeout: {timeout_marker}"
        if timeout_marker in str(exc):
            return True, f"timeout: {timeout_marker}"
        return False, ""

    def _write_status(
        self,
        status: str,
        final_iteration: Optional[int],
        reason: Optional[str],
        last_iteration_attempted: Optional[int],
        exit_reason: Optional[str] = None,
    ) -> None:
        artifacts: Dict[str, str] = {}
        for path, key in (
            (self.output_dir / "Dockerfile", "dockerfile"),
            (self.output_dir / "run_tests.sh", "run_tests"),
            (self.output_dir / "eval_script.sh", "eval_script"),
            (self.output_dir / "Dockerfile_original", "dockerfile_original"),
            (self.output_dir / "run_tests_original.sh", "run_tests_original"),
            (self.output_dir / "eval_script_original.sh", "eval_script_original"),
            (self.output_dir / "summary.json", "checker_summary"),
        ):
            if path.exists():
                artifacts[key] = self.iter_recorder.relative(path)

        status_payload = {
            "instance_id": self.task_dict.get("instance_id"),
            "status": status,
            "final_iteration": final_iteration,
            "last_iteration_attempted": last_iteration_attempted,
            "reason": reason,
            "total_iterations": len(self.iter_recorder.records),
            "aggregate": self.responses.aggregate_totals(),
            "iterations": [
                {
                    "iteration": rec.get("iteration"),
                    "status": rec.get("status"),
                    "metadata_path": rec.get("metadata_path"),
                    "cost": rec.get("cost"),
                    "llm_calls": rec.get("llm_calls"),
                    "response_log": rec.get("response_log"),
                }
                for rec in self.iter_recorder.summarize()
            ],
            "artifacts": artifacts,
            "response_logs": self.responses.list_response_logs(),
            "generated_at": utc_now(),
            "mode": self.execution_mode,
            "exit_reason": exit_reason,
        }

        status_path = self.output_dir / "status.json"
        status_path.write_text(json.dumps(status_payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------
    def __del__(self) -> None:
        try:
            if hasattr(self, "client") and self.client:
                self.client.close()
        except Exception as exc:
            self.logger.debug("TransferAgent.__del__: failed to close Docker client: %s", exc)
