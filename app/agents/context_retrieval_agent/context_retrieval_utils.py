import os
from typing import Dict, List, Any
from loguru import logger
import re
from typing import Any
from app.data_structures import MessageThread
from app.model import common
import json
import itertools
from app.prompts.prompts import (
    CONTEXT_RETRIEVAL_SYSTEM_PROMPT,
    CONTEXT_RETRIEVAL_USER_PROMPT,
)
class RepoBrowseManager:
    def __init__(self, project_path: str):
        self.project_path = os.path.abspath(project_path)  # Ensure absolute path
        self.index: Dict = {}
        self._build_index()

    def _build_index(self):
        """Build the index by parsing the repository structure."""
        self._update_index(self.project_path)

    def _update_index(self, current_path: str):
        """Recursively update the index with files and directories."""
        for root, dirs, files in os.walk(current_path):
            relative_root = os.path.relpath(root, self.project_path)
            current_level = self.index
            if relative_root != ".":  # Handle nested directories
                for part in relative_root.split(os.sep):
                    if part not in current_level:
                        current_level[part] = {}
                    current_level = current_level[part]
            for file in files:
                current_level[file] = None  # Mark files as leaf nodes

    def browse_folder(self, path: str, depth: int) -> tuple[str, str, bool]:
        """Browse a folder in the repository from the given path and depth.
        
        Args:
            path: The folder path to browse, relative to the project root
            depth: How many levels deep to browse the folder structure
            
        Returns:
            A formatted string showing the folder structure
            
        Raises:
            ValueError: If the path is outside the project directory
        """
        if not path or path == "/":
            abs_path = self.project_path
        else:
            # Check if the path is an absolute path
            if os.path.isabs(path):
                abs_path = path  # If absolute, use it directly
            else:
                # If relative path, join with project root and convert to absolute
                abs_path = os.path.abspath(os.path.join(self.project_path, path))
    

        if not abs_path.startswith(self.project_path):
            return 'Path does not exist', 'Path does not exist',False
          
        
        relative_path = os.path.relpath(abs_path, self.project_path)
        if relative_path == ".":
            current_level = self.index
        else:
            current_level = self.index
            for part in relative_path.split(os.sep):
                if part not in current_level:
                    return "Path not found", "Path not found", False  # Path not found
                current_level = current_level[part]
        
        structure_result = self._get_structure(current_level, depth)
        structure = self._format_structure(structure_result)
        result = f"You are browsing the path: {abs_path}. The browsing Depth is {depth}.\nStructure of this directory:\n\n{self._format_structure(structure_result)}"

        return result, 'folder structure collected', True


    def search_files_by_keyword(self, keyword: str) -> tuple[str, str, bool]:
        """Search for files in the repository whose names contain the given keyword.
        
        Args:
            keyword: The keyword to search for in file names
            
        Returns:
            tuple: (formatted result string, summary message, success flag)
        """
        matching_files = []
        self._search_index(self.index, keyword, "", matching_files)
        
        if not matching_files:
            return f"No files found containing the keyword '{keyword}'.", "No matching files found", True

        max_files = 50
        if len(matching_files) > max_files:
            result = f"Found {len(matching_files)} files containing the keyword '{keyword}'. Showing the first {max_files}:\n\n"
            matching_files = matching_files[:max_files]
        else:
            result = f"Found {len(matching_files)} files containing the keyword '{keyword}':\n\n"
        
        formatted_files = "\n".join([f"- {os.path.normpath(file)}" for file in matching_files])
        result += formatted_files
        return result, "File search completed successfully", True

    def _search_index(self, current_level: Dict, keyword: str, current_path: str, matching_files: List[str]):
        """Recursively search the index for files containing the keyword in their names."""
        for key, value in current_level.items():
            new_path = os.path.join(current_path, key)
            if value is None:  # It's a file
                if keyword.lower() in key.lower():
                    matching_files.append(new_path)
            else:  # It's a directory
                self._search_index(value, keyword, new_path, matching_files)

    def _get_structure(self, structure: Dict, depth: int) -> Dict:
        """Get the structure of the repository from the given path and depth."""
        if depth == 0:
            return {}
        result = {}
        for key, value in structure.items():
            if value is None:  # It's a file
                result[key] = None
            else:  # It's a directory
                result[key] = self._get_structure(value, depth - 1)
        return result

    def _format_structure(self, structure: Dict, indent: int = 0) -> str:
        """Format the structure into a string with proper indentation."""
        result = ""
        for key, value in structure.items():
            if value is None:  # It's a file
                result += "    " * indent + key + "\n\n"
            else:  # It's a directory
                result += "    " * indent + key + "/\n\n"
                result += self._format_structure(value, indent + 1)
        return result

    def browse_file(self, file_path: str) -> str:
        """
        Browse and return up to the first MAX_LINES lines of a file, wrapped in markers.

        Args:
            file_path: Path to the file relative to the project root.

        Returns:
            A string in the form:

            === FILE START: <relative path> ===
            [up to MAX_LINES lines of content]
            --- CONTENT TRUNCATED ---
            === FILE END: <relative path> ===

        Raises:
            ValueError: if the file is outside of project_path
            FileNotFoundError: if the file does not exist
        """
        abs_path = os.path.abspath(file_path)
        if not abs_path.startswith(self.project_path):
            raise ValueError(f"Path '{file_path}' is outside of project directory.")
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"File not found: '{file_path}'")

        MAX_LINES = 1000
        START_MARKER = f"=== FILE START: {file_path} ==="
        END_MARKER   = f"=== FILE END:   {file_path} ==="
        TRUNC_MARKER = "--- CONTENT TRUNCATED ---"

        with open(abs_path, 'r', encoding='utf-8') as f:
            # read up to MAX_LINES
            lines = list(itertools.islice(f, MAX_LINES))
            content = "".join(lines)
            # check if there’s more
            more = f.readline()
            if more:
                content += "\n" + TRUNC_MARKER

        return "\n".join([START_MARKER, content, END_MARKER])

    def get_webpage_content(self, url: str, timeout: int = 60) -> str:
        """Fetch and return the content of a web page using Jina Reader API.
        
        Args:
            url: The URL of the web page to fetch
            timeout: Maximum time in seconds to wait for the response (default: 10)
            
        Returns:
            The content of the web page as a string
            
        Raises:
            ValueError: If the URL is invalid or the request fails
            TimeoutError: If the request times out
        """
        if not url.startswith(('http://', 'https://')):
            raise ValueError("Invalid URL - must start with http:// or https://")
            
        jina_reader_url = f"https://r.jina.ai/{url}"
        
        try:
            response = requests.get(jina_reader_url, timeout=timeout)
            response.raise_for_status()
            
            # Validate content type
            content_type = response.headers.get('Content-Type', '')
            if 'text/html' not in content_type and 'text/plain' not in content_type:
                raise ValueError(f"Unsupported content type: {content_type}")
                
            return response.text
            
        except requests.exceptions.Timeout:
            raise TimeoutError(f"Request timed out after {timeout} seconds")
        except requests.exceptions.RequestException as e:
            raise ValueError(f"Failed to fetch web content: {str(e)}")
        
    def browse_file_for_environment_info(self, file_path: str, custom_query: str = "") -> tuple[str, str, bool]:
        """Browse a file and extract environment setup information.
        
        Args:
            repo_browse_manager: Instance for managing repo browsing.
            file_path: The path to the file to browse, relative to the project root.
            
        Returns:
            A string containing extracted environment setup info.
        """
        try:
            logger.info('entering browse')
            # Step 1: Browse the file content
            file_content = self.browse_file(file_path)
            logger.info(f"{file_content}")
            file_content = f"[File Content: {file_path}]\n{file_content}\n[/File Content]"

            # Step 2: Use LLM to extract environment information
            extracted_info = browse_file_run_with_retries(file_content, custom_query)

            # Step 3: Return extracted information
            return extracted_info,'Get File Info', True

        except ValueError as e:
            logger.info(f"Invalid file path: {str(e)}")
            return 'Invalid file path:','Invalid file path:', False
            
            # raise
        except FileNotFoundError as e:
            logger.info(f"File not found: {str(e)}")
            return 'File not found','File not found', False
            
            # raise
        except Exception as e:
            logger.info(f"Unexpected error browsing file: {str(e)}")
            return 'Unexpected error browsing file','Unexpected error browsing file', False
            
            # raise RuntimeError(f"Failed to browse file: {str(e)}") from e


    def browse_webpage_for_environment_info(self, url: str) -> str:
        """Fetch a web page and extract environment setup information.
        
        Args:
            repo_browse_manager: Instance for managing repo browsing.
            url: The URL of the web page to fetch and analyze.
            
        Returns:
            A string containing extracted environment setup info.
        """
        try:
            # Step 1: Fetch the webpage content
            webpage_content = self.get_webpage_content(url)
            
            # Step 2: Use LLM to extract environment information
            extracted_info = browse_file_run_with_retries(webpage_content, "Extract environment setup information from this webpage.")

            # Step 3: Return extracted information
            return extracted_info, 'Get Web Info', True

    
        except Exception as e:
            logger.info(f"Unexpected error browsing webpage: {str(e)}")
            return 'Unexpected error browsing web','Unexpected error browsing web', False





BROWSE_CONTENT_PROMPT = """
You are an autonomous file-browsing and analysis agent. Now the user gives you a file. Your overall mission is:
1. To review the given file content.
2. To extract any details necessary for setting up the project's environment and running its test suite.
3. To pay special attention to contents related to custom user queries.

Primary objectives:
- **Identify libraries, packages, and their exact versions.**
- List any environment variables or configuration files.
- Extract the exact commands or scripts used to run tests, including all relevant flags/options.
- **Pay special attention to commands for running individual or specific test files, not just commands for running all tests.**
- Note any prerequisites (e.g., required OS packages, language runtimes).

Formatting rules:
- Return your answer enclosed within `<analysis></analysis>` tags.
- Always wrap your structured key information in `[Key Information from <filename>] ... [/Key Information]` tags, making clear where the information was sourced (Do not use abstract path).
- Use bullet lists for clarity.
- Keep it concise and human-readable.
- Preserve original value formats (e.g., version strings, paths, flags).
- Keep the final answer concise. Do not include irrelevant information. If no relevant content is found, simply state "No relevant information found."

Example format:
<analysis>
[Key Information from README.md]
- setup command:
  - pip install -r requirements.txt
  - pip install -r requirements-dev.txt (**For development dependencies**)
  - pip install -r requirements-test.txt (**For test dependencies**)
- Libraries:
  - flask==2.0.3 (**Exact version**)
  - gunicorn==20.1.0 (**Exact version**)
  - pytest==7.1.2 (**Exact version**)
- Runtime Requirements:
  - Python >=3.8 (**Exact version**)
  - Node.js >=14.0 (**Exact version**)
  - Java >=8.0 (**Exact version**)
- Testing:
  - Test framework: pytest
  - **Test command (single test file): pytest tests/test_api.py**
- Key environment variables:
  - DEBUG=true
[/Key Information]
</analysis>
"""


def browse_file_run_with_retries(content: str, custom_query: str, retries: int=3) -> str | None:
    """Run file content analysis with retries and return the parsed <analysis> content."""
    parsed_result=None
    for idx in range(1, retries + 1):
        logger.debug("Analyzing file content. Try {} of {}", idx, retries)
        
        res_text, _ = browse_file_run(content, custom_query)

        # Extract <analysis> content if valid
        parsed_result = parse_analysis_tags(res_text)
        if parsed_result:
            logger.success("Successfully extracted environment config")
            logger.info("*"*6)
            logger.info(parsed_result)
            logger.info("*"*6)
            return parsed_result
        else:
            content += 'Please wrap result in clean xml identifier, do not use ```to wrap results. '
            logger.debug(res_text)
            logger.debug("Invalid response or missing <analysis> tags, retrying...")
    if parsed_result:
        return parsed_result
    else:
        return 'Do not get the content of the file.'


def browse_file_run(content: str, custom_query: str) -> tuple[str, MessageThread]:
    """Run the simplified content analysis agent."""
    msg_thread = MessageThread()
    msg_thread.add_system(BROWSE_CONTENT_PROMPT)
    msg_thread.add_user(f"File content:\n{content}\n")  # Truncate to prevent overflow
    msg_thread.add_user(f"Custom query from user:\n{custom_query}\n")
    try:
        res_text, *_ = common.SELECTED_MODEL.call(
            msg_thread.to_msg(), max_tokens=2048
        )
    except Exception as e:
        logger.error(f"LLM call failed in browse_file_run: {e}")
        return None, msg_thread
    msg_thread.add_model(res_text, [])
    return res_text, msg_thread


def parse_analysis_tags(data: str) -> str | None:
    """Extract and return the content within <analysis>...</analysis> tags."""
    pattern = r"<analysis>([\s\S]+?)</analysis>"
    match = re.search(pattern, data)
    if match:
        return match.group(1).strip()  # Return the content inside <analysis> tags
    return None



# Prompts are defined in app/prompts/prompts.py and imported at the top of this file.
# SYSTEM_PROMPT and USER_PROMPT are aliased here for backwards compatibility.
SYSTEM_PROMPT = CONTEXT_RETRIEVAL_SYSTEM_PROMPT
USER_PROMPT = CONTEXT_RETRIEVAL_USER_PROMPT

# ---------------------------------------------------------------------------
# Direct JSON prompts — eliminate proxy LLM overhead
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_DIRECT_JSON = """You are a context_retrieval_agent responsible for gathering precise and necessary information from the local repository to support environment setup and test execution.

Sometimes, another agent (such as a test analysis agent) may explicitly request specific information to help fix issues like Dockerfile errors or evaluation script failures.

Your primary goal is to:
- If a specific request is provided by a calling agent, focus your retrieval narrowly on that request.
- If no explicit request is given, perform a basic and limited exploration of the repository to collect general environment and test execution information.
- Pay special attention to exact versions of dependencies, setup commands, test commands, and environment config.

The repository has already been cloned locally. Be goal-driven and cost-efficient. If no tests or test configs exist, state that clearly and stop searching.

IMPORTANT: You MUST respond with a JSON object (no markdown, no explanation outside the JSON). The JSON must have these fields:
{
    "API_calls": ["api_call_1(args)", "api_call_2(args)"],
    "collected_information": "summary of all collected info so far",
    "terminate": false
}

When you have enough information, set terminate=true and provide a detailed collected_information summary.
When you need more info, set terminate=false and provide API_calls to execute.

Available APIs:
- browse_folder(path, depth): Browse folder structure. depth is a string like "1".
- browse_file_for_environment_info(file_path, custom_query): Browse a file and extract environment info.
- search_files_by_keyword(keyword): Search for files by name keyword.

API call format rules:
- All calls must be valid Python expressions: browse_folder("src", "1") NOT browse_folder(path="src", depth=1)
- browse_folder MUST include depth parameter, default "1"
- Use forward slashes for paths"""

USER_PROMPT_DIRECT_JSON = (
    "Analyze the repository and gather information needed to set up the environment and run tests. "
    "Start by inspecting key files like README.md, pyproject.toml, setup.py, requirements.txt. "
    "If you cannot find tests or test configs after a quick check, report that and stop. "
    "Respond with JSON containing API_calls, collected_information, and terminate fields."
)


# ---------------------------------------------------------------------------
# Deterministic file parsing — skip LLM for well-known file formats
# ---------------------------------------------------------------------------

def deterministic_file_parse(file_path: str) -> str | None:
    """Parse common config files deterministically, returning extracted info or None to fall back to LLM."""
    basename = os.path.basename(file_path)
    lower = basename.lower()

    if not os.path.isfile(file_path):
        return None

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read(50000)  # cap at 50KB
    except Exception:
        return None

    # requirements*.txt — just return the content as-is
    if lower.startswith('requirements') and lower.endswith('.txt'):
        return f"[Dependencies from {basename}]\n{content}\n[/Dependencies from {basename}]"

    # setup.cfg — return as-is
    if lower == 'setup.cfg':
        return f"[Setup config from {basename}]\n{content}\n[/Setup config from {basename}]"

    # pyproject.toml — return as-is (contains build system, deps, tool configs)
    if lower == 'pyproject.toml':
        return f"[Project config from {basename}]\n{content}\n[/Project config from {basename}]"

    # setup.py — return as-is
    if lower == 'setup.py':
        return f"[Setup script from {basename}]\n{content}\n[/Setup script from {basename}]"

    # Makefile — return as-is
    if lower == 'makefile':
        return f"[Build commands from {basename}]\n{content}\n[/Build commands from {basename}]"

    # .github/workflows/*.yml — return as-is (CI config)
    if file_path.endswith('.yml') or file_path.endswith('.yaml'):
        if '.github' in file_path or 'ci' in lower:
            return f"[CI config from {basename}]\n{content}\n[/CI config from {basename}]"

    # pytest.ini, tox.ini, conftest.py — return as-is
    if lower in ('pytest.ini', 'tox.ini', 'conftest.py', '.flake8', 'mypy.ini'):
        return f"[Test config from {basename}]\n{content}\n[/Test config from {basename}]"

    # For other files (README.md, CONTRIBUTING.md, etc.), fall back to LLM
    return None
