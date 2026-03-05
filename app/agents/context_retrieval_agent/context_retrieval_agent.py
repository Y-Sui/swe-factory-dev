from app.data_structures import MessageThread, FunctionCallIntent
from app.agents.context_retrieval_agent import context_retrieval_utils
import inspect
import json
from app.agents.agent import Agent
from app.task import SweTask
import os
from loguru import logger
from os.path import join as pjoin
from pathlib import Path
from app.model import common
from app.log import (
    print_acr,
    print_banner,
    print_retrieval,
)
from app.utils import parse_function_invocation
from swe_factory_utils import extract_json_from_response


class ContextRetrievalAgent(Agent):

    api_functions: list[str] = ["browse_folder", "search_files_by_keyword", "browse_file_for_environment_info"]

    def __init__(self, task: SweTask, output_dir: str, repo_basic_info: str, max_context_retrieval_round: int = 2):
        super().__init__(agent_id="ContextRetrievalAgent")
        self.msg_thread = MessageThread()
        self.task = task
        self.output_dir = os.path.abspath(output_dir)
        self.run_count = 0
        self.repo_browse_manager = context_retrieval_utils.RepoBrowseManager(self.task.project_path)
        self.root_structure = self.browse_folder('/', 1)[0]
        self.root_structure_info = f' Root directory structure of target repository: {self.root_structure}\n\n'
        self.repo_basic_info = repo_basic_info
        self.reference_setup = None

        self.max_context_retrieval_round = max_context_retrieval_round
        self.init_msg_thread()

    def init_msg_thread(self) -> None:
        self.msg_thread = MessageThread()
        self.add_system_message(context_retrieval_utils.SYSTEM_PROMPT_DIRECT_JSON)
        self.add_user_message(self.repo_basic_info)
        self.add_user_message(self.root_structure_info)
        user_prompt = context_retrieval_utils.USER_PROMPT_DIRECT_JSON
        self.add_user_message(user_prompt)

    def browse_folder(self, path: str, depth: str | int) -> tuple[str, str, bool]:
        return self.repo_browse_manager.browse_folder(path, int(depth))

    def search_files_by_keyword(self, keyword: str) -> tuple[str, str, bool]:
        return self.repo_browse_manager.search_files_by_keyword(keyword)

    def browse_file_for_environment_info(self, file_path: str, custom_query: str) -> tuple[str, str, bool]:
        try:
            if not file_path.startswith(self.task.project_path):
                file_path = pjoin(self.task.project_path, file_path)

            # Try deterministic parsing first for known file types
            extracted = context_retrieval_utils.deterministic_file_parse(file_path)
            if extracted is not None:
                return extracted, 'Get File Info (deterministic)', True

            # Fall back to LLM for other files
            extracted_info, summary, success = self.repo_browse_manager.browse_file_for_environment_info(file_path, custom_query)
            return extracted_info, summary, success

        except Exception as e:
            logger.error(f"Error while browsing file {file_path}: {e}")
            return "", f"Error extracting env info: {e}", False

    def _parse_llm_json(self, res_text: str) -> dict | None:
        """Parse the LLM response as JSON directly, no proxy needed."""
        try:
            cleaned = extract_json_from_response(res_text)
            cleaned = cleaned.lstrip('```json').rstrip('```')
            data = json.loads(cleaned)
            if not isinstance(data, dict):
                return None
            # Validate required fields
            if "terminate" not in data:
                return None
            if data["terminate"] and not data.get("collected_information"):
                return None
            if not data["terminate"] and not isinstance(data.get("API_calls", []), list):
                return None
            return data
        except (json.JSONDecodeError, ValueError, TypeError):
            return None

    def run_task(self, print_callback=None) -> tuple[str | None, str | None, bool | None]:
        self.run_count += 1
        context_retrieval_round = -1
        task_output = None
        summary = None
        success = None

        self.reset_tool_sequence()
        while True:
            context_retrieval_round += 1

            context_retrieval_output_dir = self.get_latest_context_retrieval_output_dir()
            os.makedirs(context_retrieval_output_dir, exist_ok=True)
            conversation_file = pjoin(context_retrieval_output_dir, f"conversation_{context_retrieval_round}.json")
            self.msg_thread.save_to_file(conversation_file)

            print_banner(f"Task {self.task.task_id} Iteration ROUND {self.iteration_num} CONTEXT RETRIEVAL ROUND {context_retrieval_round}")
            print_acr('context retrieval', f"context retrieval {context_retrieval_round}", print_callback=print_callback)

            # Direct JSON call — no proxy agent needed
            try:
                res_text, *_ = common.SELECTED_MODEL.call(
                    self.msg_thread.to_msg(),
                    response_format="json_object",
                    max_tokens=2048,
                )
            except Exception as e:
                logger.error(f"LLM call failed in context retrieval round {context_retrieval_round}: {e}")
                break
            self.add_model_message(res_text, tools=[])
            print_retrieval(res_text, f"context retrieval {context_retrieval_round}", print_callback=print_callback)

            # Parse JSON directly
            parsed = self._parse_llm_json(res_text)
            if parsed is None:
                msg = "Your response was not valid JSON. Please respond with a JSON object containing 'API_calls', 'collected_information', and 'terminate' fields."
                self.add_user_message(msg)
                print_acr(msg, f"context retrieval {context_retrieval_round}", print_callback=print_callback)
                if context_retrieval_round >= self.max_context_retrieval_round:
                    task_output = None
                    summary = "Collect context information failure."
                    success = False
                    break
                continue

            json_api_calls = parsed.get("API_calls", [])
            is_termination = parsed.get("terminate", False)
            summary_of_collected_information = parsed.get("collected_information", None)

            if is_termination:
                msg_summary = f'Collected information from context retrieval agent:\n{summary_of_collected_information}\n\n'
                task_output = msg_summary
                summary = "Collect context information successfully."
                success = True
                break

            formatted = []
            if json_api_calls:
                formatted.append("API calls:")
                for call in json_api_calls:
                    formatted.extend([f"\n- `{call}`"])
            print_acr("\n".join(formatted), "Agent-selected API calls", print_callback=print_callback)

            # Execute API calls
            collated_tool_response = ""
            for api_call in json_api_calls:
                try:
                    func_name, func_args = parse_function_invocation(api_call)
                    arg_spec = inspect.getfullargspec(getattr(context_retrieval_utils.RepoBrowseManager, func_name))
                    arg_names = arg_spec.args[1:]
                    assert len(func_args) == len(arg_names), f"Number of argument is wrong in API call: {api_call}"
                    kwargs = dict(zip(arg_names, func_args))
                    intent = FunctionCallIntent(func_name, kwargs, None)
                except Exception as call_api_e:
                    collated_tool_response += f"Exception when calling {api_call}: {call_api_e}\n\n"
                    continue
                tool_output, _, _ = self.dispatch_intent(intent)
                collated_tool_response += f"Result of {api_call}:\n\n"
                collated_tool_response += f'{tool_output}\n\n'

            self.add_user_message(collated_tool_response)
            print_acr(collated_tool_response, f"context retrieval {context_retrieval_round}", print_callback=print_callback)

            if context_retrieval_round < self.max_context_retrieval_round:
                msg = (
                    "Analyze the collected information. Respond with JSON:\n"
                    "- If sufficient: set terminate=true and provide collected_information summary.\n"
                    "- If more info needed: set terminate=false and provide API_calls.\n"
                    "Preserve original content and indicate sources in the summary."
                )
                self.add_user_message(msg)
                print_acr(msg, f"context retrieval {context_retrieval_round}", print_callback=print_callback)
            else:
                task_output = None
                summary = "Collect context information failure."
                success = False
                break

        self.dump_tool_sequence(self.get_latest_context_retrieval_output_dir())
        self.init_msg_thread()
        return task_output, summary, success

    def _read_file(self, path: str) -> str:
        try:
            with open(path, 'r') as f:
                return f.read()
        except Exception:
            return ""

    def get_latest_context_retrieval_output_dir(self) -> str:
        return os.path.join(self.output_dir, f"context_retrieval_agent_{self.run_count}")

    def browse_readme(self) -> str:
        readme_list = ["README.md", "README.rst", "README.txt"]
        for readme_name in readme_list:
            file_path = pjoin(self.task.project_path, readme_name)
            try:
                readme_content = self.repo_browse_manager.browse_file(file_path)
                return f"The content of {readme_name} in the target repository:\n<README>\n{readme_content}\n</README>\n"
            except Exception:
                continue
        return ""
