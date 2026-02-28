"""Container management, tooling, and verification utilities."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from docker.models.containers import Container

from .utils.errors import CommandError, EvalNoExitCodeError, EvalTimeoutError
from .utils.logging_utils import configure_file_logger, utc_now


# ---------------------------------------------------------------------------
# Shared tooling configuration
# ---------------------------------------------------------------------------


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m|\r", "", text)


DEFAULT_TOOL_NAMES = ["search", "file_editor", "finish", "execute_bash"]

from swe_factory_utils import extract_exit_code


# ---------------------------------------------------------------------------
# Container checker / manager
# ---------------------------------------------------------------------------



class ContainerChecker:
    """Wrapper around a Docker container with validation and execution helpers."""

    def __init__(
        self,
        workdir: str = "/testbed",
        logger: logging.Logger | None = None,
        log_file: str | None = None,
        runtime: DockerRuntime | None = None,
        tools_to_check: Optional[List[str]] = None,
        gold_patch: Optional[str] = None,
    ) -> None:
        self.workdir = workdir
        if logger is not None:
            self.logger = logger
        else:
            self.logger = logging.getLogger(f"{__name__}.{id(self)}")

        if log_file:
            configure_file_logger(self.logger, log_file)
        self.logger.setLevel(logging.INFO)

        self.container: Container | None = None
        self.runtime: DockerRuntime | None = runtime
        self.commands_ok: bool = False
        self.eval_ok: bool = False
        self.eval_output: str | None = None
        self._log_records: List[str] = []
        self.command_checks: List[Dict[str, Any]] = []
        self.tools_to_check = tools_to_check or DEFAULT_TOOL_NAMES
        self.eval_script_path: Optional[str] = None
        self.gold_patch = gold_patch if gold_patch and gold_patch.strip() else None
        self._git_sanitized = False

    # ------------------------------------------------------------------
    # Basic logging helpers
    # ------------------------------------------------------------------
    def _log(self, message: str) -> None:
        self.logger.info(message)
        self._log_records.append(message)

    # ------------------------------------------------------------------
    # Container attachments
    # ------------------------------------------------------------------
    def set_container(self, container: Container) -> None:
        self.container = container
        self._maybe_sanitize_git()

    def set_runtime(self, runtime: DockerRuntime) -> None:
        self.runtime = runtime

    def ensure_container(self) -> Container:
        if self.container is None:
            raise RuntimeError("Container not attached yet")
        return self.container

    def ensure_runtime(self) -> DockerRuntime:
        if self.runtime is None:
            raise RuntimeError("Docker runtime not attached yet")
        return self.runtime

    def _maybe_sanitize_git(self) -> None:
        if self._git_sanitized or self.container is None:
            return
        try:
            self.run_cmd("git config core.filemode false", workdir=self.workdir)
            self.run_cmd(
                "printf 'testbed/\\n.venv/\\nvenv/\\n__pycache__/\\npytest_cache/\\n*.egg-info\\n' >> .git/info/exclude || true",
                workdir=self.workdir,
            )
        except Exception as exc:  # pragma: no cover - best-effort hygiene
            self.logger.warning(f"Failed to sanitize git settings: {exc}")
        else:
            self._git_sanitized = True

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------
    def check_commands(self) -> Tuple[bool, List]:
        container = self.ensure_container()

        self._log("[CHECK] Tool command success")
        tools = self.tools_to_check
        failures = []
        self.command_checks = []
        for tool in tools:
            attempts = []
            ok = False
            success_cmd: str | None = None
            for flag in ("--help", "-h"):
                cmd = f"{tool} {flag}"
                code, output = self.run_cmd(cmd, workdir=self.workdir)
                self._log(f"[{cmd}] exit={code}\n{output}")
                attempts.append({"cmd": cmd, "exit_code": code, "output": output})
                if code == 0:
                    ok = True
                    success_cmd = cmd
                    break
            if not ok:
                first_attempt = attempts[0] if attempts else {}
                failures.append(
                    {
                        "tool": tool,
                        "cmd": first_attempt.get("cmd", tool),
                        "exit_code": first_attempt.get("exit_code"),
                        "output": first_attempt.get("output", ""),
                        "tried": attempts,
                    }
                )
            self.command_checks.append(
                {
                    "tool": tool,
                    "status": "ok" if ok else "failed",
                    "attempts": attempts,
                    "successful_cmd": success_cmd,
                }
            )
        if failures:
            raise CommandError(failures)
        self.commands_ok = True
        return True, []

    # ------------------------------------------------------------------
    # Command execution primitives
    # ------------------------------------------------------------------
    def run_cmd(
        self,
        cmd: str,
        workdir: str | None = None,
        timeout: int = 300,
        extra_env: Dict[str, Any] | None = None,
    ) -> Tuple[int, str]:
        container = self.ensure_container()
        effective_workdir = workdir if workdir is not None else self.workdir
        return self.run_cmd_static(
            container,
            cmd,
            workdir=effective_workdir,
            timeout=timeout,
            extra_env=extra_env,
        )

    @staticmethod
    def run_cmd_static(
        container: Container,
        cmd: str,
        workdir: str = "/",
        timeout: int = 300,
        extra_env: Dict[str, Any] | None = None,
    ) -> Tuple[int, str]:
        bash_cmd = f"timeout {timeout} {cmd}"
        docker_cmd = ["/bin/bash", "-lc", bash_cmd]

        env_kwargs: Dict[str, Any] = {}
        if extra_env:
            env_kwargs["environment"] = extra_env

        def _exec():
            return container.exec_run(
                cmd=docker_cmd,
                stdout=True,
                stderr=True,
                workdir=workdir,
                **env_kwargs,
            )

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_exec)
                result = future.result(timeout=timeout + 10)
            output = result.output.decode("utf-8", errors="replace")
            exit_code = result.exit_code

            if exit_code == 124:
                return exit_code, f"The command took too long to execute (>{timeout}s)"

            return exit_code, _strip_ansi(output)

        except concurrent.futures.TimeoutError:
            return -1, f"Timeout: The command took too long to execute (>{timeout}s)"
        except Exception as exc:  # pragma: no cover - defensive path
            return -1, f"Error: {exc!r}"

    def copy_to_container(self, local_path: Path, dest_path: str) -> None:
        runtime = self.ensure_runtime()
        runtime.copy_to_container(str(local_path), dest_path)

    # ------------------------------------------------------------------
    # High-level workflows
    # ------------------------------------------------------------------
    def run_eval_script(
        self,
        local_script: Path | None,
        iteration_dir: Path,
        log_name: str,
        dest_path: str = "/eval_script.sh",
        timeout: int = 300,
        copy_from_host: bool = True,
    ) -> Dict[str, Any]:
        log_path = iteration_dir / log_name
        log_sections: List[str] = []

        def _add_log_section(title: str, exit_code: int, text: str, shell_exit: int | None = None) -> None:
            lines: List[str] = []
            if shell_exit is not None:
                lines.append(f"shell_exit_code={shell_exit}")
            stripped = text.rstrip()
            if stripped:
                lines.append(stripped)
            section = f"[{title}] exit_code={exit_code}"
            if lines:
                section = section + "\n" + "\n".join(lines)
            log_sections.append(section)

        def _flush_log() -> None:
            if not log_sections:
                return
            with log_path.open("w", encoding="utf-8") as handle:
                handle.write("\n\n".join(log_sections))
                handle.write("\n")

        if copy_from_host:
            if local_script is None:
                raise ValueError("local_script path required when copy_from_host is True")
            self.copy_to_container(local_script, dest_path)
        self.eval_script_path = dest_path

        if self.gold_patch:
            patch_text = self.gold_patch
            if not patch_text.endswith("\n"):
                patch_text += "\n"
            patch_host_path = iteration_dir / "gold_patch.diff"
            patch_host_path.write_text(patch_text, encoding="utf-8")
            patch_container_path = "/tmp/gold_patch.diff"
            self.copy_to_container(patch_host_path, patch_container_path)
            patch_code, patch_output = self.run_cmd(
                f"git apply -v {patch_container_path}",
                workdir="/testbed",
                timeout=120,
            )
            _add_log_section("gold_patch", patch_code, patch_output)
            if patch_code != 0:
                _flush_log()
                raise CommandError(
                    [
                        {
                            "cmd": "git apply -v /tmp/gold_patch.diff",
                            "exit_code": patch_code,
                            "output": patch_output,
                        }
                    ]
                )

        code, output = self.run_cmd(
            f"bash {dest_path}",
            workdir="/",
            timeout=timeout,
        )
        omni_exit = extract_exit_code(output)
        _add_log_section("eval_script", omni_exit if omni_exit is not None else code, output, shell_exit=code)

        if code == 124 or (code == -1 and "Timeout" in output):
            _flush_log()
            raise EvalTimeoutError(timeout)
        if omni_exit is None:
            _flush_log()
            raise EvalNoExitCodeError(output)
        if omni_exit != 0:
            _flush_log()
            raise CommandError(
                [
                    {
                        "cmd": dest_path,
                        "exit_code": omni_exit,
                        "output": output,
                    }
                ]
            )

        _flush_log()

        self.eval_ok = True
        self.eval_output = output
        return {
            "exit_code": omni_exit,
            "shell_exit_code": code,
            "output": output,
            "log_path": log_path,
        }


    # ------------------------------------------------------------------
    # Reporting helpers
    # ------------------------------------------------------------------
    def record_eval(self, result: Tuple[bool, str]) -> None:
        success, output = result
        self.eval_ok = success
        self.eval_output = output
        script_label = Path(self.eval_script_path).name if self.eval_script_path else "eval_script.sh"
        self._log(f"[CHECK] {script_label} results，success={success}\n{output}")

    def summary(self) -> Dict[str, Any]:
        return {
            "commands_ok": self.commands_ok,
            "eval_ok": self.eval_ok,
            "eval_output": self.eval_output,
        }

    def dump_state(self, base: Path) -> None:
        base.mkdir(parents=True, exist_ok=True)
        payload = {"summary": self.summary(), "log_records": self._log_records}
        (base / "checker_state.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
