"""
A proxy agent. Process raw response into json format.
"""

from typing import Any
import re
from loguru import logger
from collections.abc import Callable
from app.data_structures import MessageThread
from app.model import common
from app.post_process import ExtractStatus, is_valid_json
import json

SYSTEM_PROMPT = """You are an expert in analyzing and validating evaluation environment setups for software testing.

Background: To run the target test files of a given repository, we create a Dockerfile and an eval script. The eval script is invoked inside the container built by that Dockerfile.
Important: The WriteTestAgent may generate new test files whose parent directories may not exist in the original repo. Do NOT treat missing test directories as a blocking error. Instead, ensure the eval script creates directories (e.g., `mkdir -p`) before applying the test patch, and only flag issues if the patch fails to apply or tests fail.

If Docker test execution results are available, you will also receive:
- Post-patch test log (with gold patch applied)
- Pre-patch test log (without gold patch)
- F2P (Fail-to-Pass) classification result

The F2P classification tells you whether the generated tests properly capture the bug:
- **FAIL2PASS**: Tests fail without patch, pass with patch — this is the desired outcome.
- **PASS2PASS**: Tests pass both times — tests are too weak and do not detect the bug.
- **FAIL2FAIL**: Tests fail both times — likely an environment or test setup issue.
- **PASS2FAIL**: Tests pass without patch but fail with it — tests are broken or inverted.
- **ERROR**: Could not determine exit codes from one or both runs.

If Docker results are NOT available, perform static analysis of the Dockerfile and eval script.

Your task:
1. Determine whether the evaluation environment is correctly set up.
2. If issues exist, diagnose whether they come from:
   - The **Dockerfile** (environment setup issues)
   - The **evaluation script** (test execution issues)
   - The **generated test files** (test content issues)
   - Missing information that needs to be collected
3. Provide **clear guidance** to the appropriate agent:
   - `write_dockerfile_agent`
   - `write_eval_script_agent`
   - `write_test_agent`
   - `context_retrieval_agent`

Your findings must be structured in JSON format."""

ANALYZE_PROMPT = """
Analyze the current evaluation environment setup and determine whether it is valid.

### **Step 1: Verify Test Execution**
If test logs are available:
- Identify which test files were added or modified by the eval script.
- Confirm that those tests were actually executed (they appear in the test log).
- Check their pass/fail status: if all target tests passed, report success.
- If no test output is found at all, set `is_finish = false` and instruct write_eval_script_agent to fix the eval script.

If test logs are NOT available (static analysis):
- Verify the Dockerfile clones the repo and installs dependencies.
- Verify the eval script invokes a test runner on the correct test files.
- Verify the eval script captures the exit code and echoes `OMNIGRIL_EXIT_CODE=$rc`.
- Check for syntax errors, missing dependencies, or incorrect commands.

### **Step 1.5: F2P Validation** (only when F2P classification is provided)
Use the F2P result to guide your diagnosis:
  - **FAIL2PASS**: Desired outcome. Set `is_finish = true` if the post-patch run passed cleanly.
  - **PASS2PASS**: Tests do not capture the bug. Provide guidance to `write_test_agent`. Check whether the tests mock the very function the patch modifies — if so, instruct `write_test_agent` to remove that mock and call the real code.
  - **FAIL2FAIL**: Likely an environment/setup issue. Provide guidance to `write_dockerfile_agent` and/or `write_eval_script_agent`.
  - **PASS2FAIL**: Tests are broken or inverted. Provide guidance to `write_test_agent` to fix the test logic.
  - **ERROR**: Ensure the eval script echoes `OMNIGRIL_EXIT_CODE=$rc` properly.

### **Step 2: Identify Problems**
- If tests failed due to environment issues, determine whether the **Dockerfile** or **eval script** is at fault.
- Check versions of critical dependencies for compatibility.
- Tests should NOT be run in the Dockerfile; they belong in the eval script.
- The eval script MUST echo `OMNIGRIL_EXIT_CODE=$rc` after running tests.

### **Step 3: Plan Corrective Actions**
- For **Dockerfile** issues: provide guidance to `write_dockerfile_agent` with the original error message.
- For **eval script** issues: provide guidance to `write_eval_script_agent` with the original error message.
- For **test file** issues: provide guidance to `write_test_agent` on how to improve them.
  - **PASS2PASS diagnosis**: Before sending generic "strengthen the test" guidance, check whether the tests mock the very function or class that the patch modifies. If so, that is the root cause — the test calls a mock instead of the real implementation and will always pass. Instruct `write_test_agent` to remove that mock and call the real code instead.
- If more repo context is needed: provide guidance to `context_retrieval_agent`. Be specific about which files to look for (e.g., requirements.txt, pyproject.toml, pytest.ini, .github/workflows/*). Only request context retrieval when clearly necessary — it is expensive.

### **Output**
Provide your answer in JSON format:
```json
{
    "is_finish": true/false,
    "guidance_for_write_dockerfile_agent": "",
    "guidance_for_write_eval_script_agent": "",
    "guidance_for_write_test_agent": "",
    "guidance_for_context_retrieval_agent": ""
}
```

**Important:**
- If `is_finish` is `true`, all guidance fields should be empty.
- If `is_finish` is `false`, at least one guidance field must be non-empty with specific, actionable steps.
- Include original error messages in guidance to help agents understand what went wrong.
"""


def run_with_retries(msg_thread: MessageThread, retries=3, print_callback: Callable[[dict], None] | None = None):

    for idx in range(1, retries + 1):
        logger.debug(
            "Trying to analyze the test log. Try {} of {}.", idx, retries
        )

        res_text = run(msg_thread)
        if res_text is None:
            logger.debug("LLM call returned None. Will retry.")
            continue
        res_text = extract_json_from_response(res_text)
        res_text = res_text.lstrip('```json').rstrip('```')
        logger.debug(res_text)
        extract_status, data = is_valid_json(res_text)

        if extract_status != ExtractStatus.IS_VALID_JSON:
            logger.debug("Invalid json. Will retry.")
            continue

        valid, diagnosis = is_valid_response(data)
        if not valid:
            logger.debug(f"{diagnosis}. Will retry.")
            continue

        logger.debug("Extracted a valid json")
        return res_text
    return None


def run(msg_thread: MessageThread):
    """
    Run the agent to extract issue to json format.
    """
    msg_thread.add_user(ANALYZE_PROMPT)
    try:
        res_text, *_ = common.SELECTED_MODEL.call(
            msg_thread.to_msg(), response_format="json_object"
        )
    except Exception as e:
        logger.error(f"LLM call failed in test analysis: {e}")
        return None

    msg_thread.add_model(res_text, [])  # no tools

    return res_text


def is_valid_response(data: Any) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "Json is not a dict"

    if not data.get("is_finish"):
        terminate = data.get("is_finish")
        if terminate is None:
            return False, "'is_finish' parameter is missing"

        if not isinstance(terminate, bool):
            return False, "'is_finish' parameter must be a boolean (true/false)"

    # When is_finish is true, guidance fields are not needed — skip validation.
    if data.get("is_finish") is True:
        return True, "OK"

    key_list = [
        'guidance_for_write_dockerfile_agent',
        'guidance_for_write_eval_script_agent',
        'guidance_for_write_test_agent',
        'guidance_for_context_retrieval_agent',
    ]
    # When is_finish is False, at least one guidance field must be non-empty
    has_guidance = False
    for key in key_list:
        val = data.get(key)
        if val and isinstance(val, str) and val.strip():
            has_guidance = True
            break

    if not has_guidance:
        return False, "At least one guidance field must be non-empty when is_finish is False"

    return True, "OK"



def extract_json_from_response(res_text: str):
    """
    Extarct json result from the LLM response
    """
    json_extracted = None


    json_matches = re.findall(r"```json([\s\S]*?)```", res_text, re.IGNORECASE)
    if json_matches:
        json_extracted = json_matches[0].strip()


    if not json_extracted:
        json_code_blocks = re.findall(r"```([\s\S]*?)```", res_text, re.IGNORECASE)
        for content in json_code_blocks:
            clean_content = content.strip()

            try:
                json.loads(clean_content)
                json_extracted = clean_content
                break
            except json.JSONDecodeError:
                continue

    return json_extracted if json_extracted else res_text
