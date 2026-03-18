"""Fix problematic test files in instances.jsonl and validate in Docker."""
import json
import subprocess
import tempfile
from pathlib import Path

INSTANCES_PATH = Path("swe-smith-dev/output/swe_smith_instances.jsonl")

# Docker config per repo
DOCKER_CONFIG = {
    "MiroMindAI/miroflow": {
        "image_tag": "internal-swe-bench-miroflow:base",
        "test_dir": "/testbed",
        "pytest_cmd": "uv run pytest",
    },
    "MiroMindAI/MiroThinker": {
        "image_tag": "internal-swe-bench-mirothinker:base",
        "test_dir": "/testbed/apps/miroflow-agent",
        "pytest_cmd": "uv run pytest -o 'addopts='",
    },
    "MiroMindAI/sd-torchtune": {
        "image_tag": "internal-swe-bench-sd-torchtune:base",
        "test_dir": "/testbed",
        "pytest_cmd": "pytest",
    },
}

# ── Replacement tests ────────────────────────────────────────────────

FIXES = {}

# [15] MiroThinker-smith-0003: extract_tool_calls — was 66 mocks, rewrite with real parsing
# vLLM is not installed, so we must mock vllm modules before importing the parser
FIXES["MiroMindAI__MiroThinker-smith-0003"] = '''\
import sys
import os
import json
from unittest.mock import MagicMock
from types import ModuleType

# Mock vLLM modules before importing the parser
_vllm_mods = {}
for mod_name in [
    "vllm", "vllm.entrypoints", "vllm.entrypoints.chat_utils",
    "vllm.entrypoints.openai", "vllm.entrypoints.openai.protocol",
    "vllm.entrypoints.openai.tool_parsers",
    "vllm.entrypoints.openai.tool_parsers.abstract_tool_parser",
    "vllm.logger",
]:
    m = ModuleType(mod_name)
    _vllm_mods[mod_name] = m
    sys.modules[mod_name] = m

# Provide minimal real types the parser uses
class _FunctionCall:
    def __init__(self, name="", arguments=""):
        self.name = name
        self.arguments = arguments

class _ToolCall:
    def __init__(self, type="function", function=None):
        self.type = type
        self.function = function or _FunctionCall()

class _ExtractedToolCallInformation:
    def __init__(self, tools_called=False, tool_calls=None, content=None):
        self.tools_called = tools_called
        self.tool_calls = tool_calls or []
        self.content = content

class _DeltaFunctionCall:
    def __init__(self, name="", arguments=""):
        self.name = name
        self.arguments = arguments
    def model_dump(self, exclude_none=False):
        return {"name": self.name, "arguments": self.arguments}

class _DeltaToolCall:
    def __init__(self, index=0, type="function", id="", function=None):
        self.index = index
        self.type = type
        self.id = id
        self.function = function

class _DeltaMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

class _ChatCompletionRequest:
    pass

class _ToolParser:
    def __init__(self, tokenizer):
        self.model_tokenizer = tokenizer

class _ToolParserManager:
    @staticmethod
    def register_module(*a, **kw):
        pass

_vllm_mods["vllm.entrypoints.chat_utils"].make_tool_call_id = lambda: "call_123"
proto = _vllm_mods["vllm.entrypoints.openai.protocol"]
proto.ChatCompletionRequest = _ChatCompletionRequest
proto.DeltaFunctionCall = _DeltaFunctionCall
proto.DeltaMessage = _DeltaMessage
proto.DeltaToolCall = _DeltaToolCall
proto.ExtractedToolCallInformation = _ExtractedToolCallInformation
proto.FunctionCall = _FunctionCall
proto.ToolCall = _ToolCall
atp = _vllm_mods["vllm.entrypoints.openai.tool_parsers.abstract_tool_parser"]
atp.ToolParser = _ToolParser
atp.ToolParserManager = _ToolParserManager
_vllm_mods["vllm.logger"].init_logger = lambda *a: __import__("logging").getLogger("test")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lobehub-compatibility"))
from MiroThinkerToolParser import MirothinkerToolParser


def _make_parser():
    return MirothinkerToolParser(MagicMock())


def _make_request(tool_names):
    req = MagicMock()
    tools = []
    for name in tool_names:
        tool = MagicMock()
        tool.function.name = name
        tools.append(tool)
    req.tools = tools
    req.tool_choice = "auto"
    return req


def test_extract_single_tool_call():
    parser = _make_parser()
    output = """Hello.
<use_mcp_tool>
<server_name>default</server_name>
<tool_name>search</tool_name>
<arguments>{"query": "hello"}</arguments>
</use_mcp_tool>"""
    result = parser.extract_tool_calls(output, _make_request(["search"]))
    assert result.tools_called is True
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].function.name == "search"
    args = json.loads(result.tool_calls[0].function.arguments)
    assert "query" in args


def test_extract_multiple_tool_calls():
    parser = _make_parser()
    output = """<use_mcp_tool>
<server_name>default</server_name>
<tool_name>read</tool_name>
<arguments>{"p": "a"}</arguments>
</use_mcp_tool>
<use_mcp_tool>
<server_name>default</server_name>
<tool_name>write</tool_name>
<arguments>{"p": "b"}</arguments>
</use_mcp_tool>"""
    result = parser.extract_tool_calls(output, _make_request(["read", "write"]))
    assert result.tools_called is True
    assert len(result.tool_calls) == 2


def test_extract_no_tool_calls():
    parser = _make_parser()
    output = "Just a regular response."
    result = parser.extract_tool_calls(output, _make_request(["search"]))
    assert result.tools_called is False
    assert result.content == output


def test_extract_preserves_content_before_tool():
    parser = _make_parser()
    output = """Let me search.
<use_mcp_tool>
<server_name>default</server_name>
<tool_name>search</tool_name>
<arguments>{"q": "x"}</arguments>
</use_mcp_tool>"""
    result = parser.extract_tool_calls(output, _make_request(["search"]))
    assert result.tools_called is True
    assert "Let me search" in result.content


def test_extract_tool_choice_none():
    parser = _make_parser()
    output = """<use_mcp_tool>
<server_name>default</server_name>
<tool_name>search</tool_name>
<arguments>{"q": "x"}</arguments>
</use_mcp_tool>"""
    req = _make_request(["search"])
    req.tool_choice = "none"
    result = parser.extract_tool_calls(output, req)
    assert result.tools_called is False
'''

# [16] MiroThinker-smith-0004: _resolve_tool_name — was 60 mocks, rewrite with vllm mocking
FIXES["MiroMindAI__MiroThinker-smith-0004"] = '''\
import sys
import os
from unittest.mock import MagicMock
from types import ModuleType

# Mock vLLM modules before import
for mod_name in [
    "vllm", "vllm.entrypoints", "vllm.entrypoints.chat_utils",
    "vllm.entrypoints.openai", "vllm.entrypoints.openai.protocol",
    "vllm.entrypoints.openai.tool_parsers",
    "vllm.entrypoints.openai.tool_parsers.abstract_tool_parser",
    "vllm.logger",
]:
    sys.modules[mod_name] = ModuleType(mod_name)

class _ToolParser:
    def __init__(self, tokenizer):
        self.model_tokenizer = tokenizer

class _ToolParserManager:
    @staticmethod
    def register_module(*a, **kw):
        pass

sys.modules["vllm.entrypoints.openai.tool_parsers.abstract_tool_parser"].ToolParser = _ToolParser
sys.modules["vllm.entrypoints.openai.tool_parsers.abstract_tool_parser"].ToolParserManager = _ToolParserManager
sys.modules["vllm.entrypoints.chat_utils"].make_tool_call_id = lambda: "call_123"
sys.modules["vllm.logger"].init_logger = lambda *a: __import__("logging").getLogger("test")
# Provide protocol stubs
proto = sys.modules["vllm.entrypoints.openai.protocol"]
for cls_name in ["ChatCompletionRequest", "DeltaFunctionCall", "DeltaMessage",
                 "DeltaToolCall", "ExtractedToolCallInformation", "FunctionCall", "ToolCall"]:
    setattr(proto, cls_name, type(cls_name, (), {}))

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lobehub-compatibility"))
from MiroThinkerToolParser import MirothinkerToolParser


def _make_parser():
    return MirothinkerToolParser(MagicMock())


def _make_request(tool_names):
    req = MagicMock()
    tools = []
    for name in tool_names:
        tool = MagicMock()
        tool.function.name = name
        tools.append(tool)
    req.tools = tools
    return req


def test_resolve_default_server():
    parser = _make_parser()
    result = parser._resolve_tool_name("default", "search", _make_request(["srv__search"]))
    assert result == "search"


def test_resolve_empty_server():
    parser = _make_parser()
    result = parser._resolve_tool_name("", "search", _make_request(["search"]))
    assert result == "search"


def test_resolve_single_candidate():
    parser = _make_parser()
    result = parser._resolve_tool_name("my_srv", "search", _make_request(["my_srv__search", "other__read"]))
    assert result == "my_srv__search"


def test_resolve_disambiguation_by_server():
    parser = _make_parser()
    result = parser._resolve_tool_name("srv_a", "search", _make_request(["srv_a__search", "srv_b__search"]))
    assert result == "srv_a__search"


def test_resolve_no_match():
    parser = _make_parser()
    result = parser._resolve_tool_name("srv", "missing", _make_request(["read", "write"]))
    assert result == "missing"


def test_resolve_caches():
    parser = _make_parser()
    r1 = parser._resolve_tool_name("srv", "tool", _make_request(["srv__tool"]))
    r2 = parser._resolve_tool_name("srv", "tool", _make_request(["srv__tool"]))
    assert r1 == r2
    assert ("srv", "tool") in parser._resolved_tool_name_cache
'''

# [4] miroflow-smith-0004: execute_tool_call — keep mocks but simplify drastically
FIXES["MiroMindAI__miroflow-smith-0004"] = '''\
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from miroflow.tool.manager import ToolManager


def test_execute_tool_call_server_not_found_no_alternatives():
    """When server doesn't exist and tool not found elsewhere, return error dict."""
    manager = ToolManager(server_configs=[])

    async def run():
        with patch.object(manager, '_find_servers_with_tool', new_callable=AsyncMock, return_value=[]):
            result = await manager.execute_tool_call(
                server_name="nonexistent",
                tool_name="my_tool",
                arguments={}
            )
        assert isinstance(result, dict)
        assert "error" in result

    asyncio.run(run())


def test_execute_tool_call_autocorrects_server():
    """When tool found on a different server, auto-correct and call it."""
    from mcp import StdioServerParameters
    manager = ToolManager(server_configs=[
        {"name": "real_server", "params": StdioServerParameters(command="echo", args=[], env={})}
    ])

    mock_result_content = MagicMock()
    mock_result_content.text = "tool output"
    mock_result = MagicMock()
    mock_result.content = [mock_result_content]

    mock_session = AsyncMock()
    mock_session.initialize = AsyncMock()
    mock_session.call_tool = AsyncMock(return_value=mock_result)

    async def run():
        with patch.object(manager, '_find_servers_with_tool', new_callable=AsyncMock, return_value=["real_server"]), \\
             patch('miroflow.tool.manager.stdio_client') as mock_stdio, \\
             patch('miroflow.tool.manager.ClientSession') as mock_cs:
            mock_stdio.return_value.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
            mock_stdio.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await manager.execute_tool_call(
                server_name="wrong_server",
                tool_name="my_tool",
                arguments={"key": "val"}
            )
        assert isinstance(result, dict)
        assert "result" in result
        # Should mention auto-correction
        assert "Auto-corrected" in result.get("result", "") or "real_server" in str(result)

    asyncio.run(run())


def test_execute_tool_call_returns_result_on_success():
    """Successful tool call returns result dict with tool output."""
    from mcp import StdioServerParameters
    manager = ToolManager(server_configs=[
        {"name": "srv", "params": StdioServerParameters(command="echo", args=[], env={})}
    ])

    mock_result_content = MagicMock()
    mock_result_content.text = "42"
    mock_result = MagicMock()
    mock_result.content = [mock_result_content]

    mock_session = AsyncMock()
    mock_session.initialize = AsyncMock()
    mock_session.call_tool = AsyncMock(return_value=mock_result)

    async def run():
        with patch('miroflow.tool.manager.stdio_client') as mock_stdio, \\
             patch('miroflow.tool.manager.ClientSession') as mock_cs:
            mock_stdio.return_value.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
            mock_stdio.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await manager.execute_tool_call(
                server_name="srv",
                tool_name="compute",
                arguments={"x": 1}
            )
        assert isinstance(result, dict)
        assert "result" in result

    asyncio.run(run())


def test_execute_tool_call_handles_exception():
    """When tool call raises, result should contain error info."""
    from mcp import StdioServerParameters
    manager = ToolManager(server_configs=[
        {"name": "srv", "params": StdioServerParameters(command="echo", args=[], env={})}
    ])

    mock_session = AsyncMock()
    mock_session.initialize = AsyncMock()
    mock_session.call_tool = AsyncMock(side_effect=Exception("connection failed"))

    async def run():
        with patch('miroflow.tool.manager.stdio_client') as mock_stdio, \\
             patch('miroflow.tool.manager.ClientSession') as mock_cs:
            mock_stdio.return_value.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
            mock_stdio.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=None)

            result = await manager.execute_tool_call(
                server_name="srv",
                tool_name="broken",
                arguments={}
            )
        assert isinstance(result, dict)
        # Should contain error information
        assert "error" in result or "Error" in str(result.get("result", ""))

    asyncio.run(run())
'''

# [6] miroflow-smith-0006: _find_servers_with_tool — simplify mock structure
FIXES["MiroMindAI__miroflow-smith-0006"] = '''\
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from mcp import StdioServerParameters
from miroflow.tool.manager import ToolManager


def _make_manager(server_names):
    """Create a ToolManager with named stdio servers."""
    configs = [
        {"name": name, "params": StdioServerParameters(command="echo", args=[], env={})}
        for name in server_names
    ]
    return ToolManager(server_configs=configs)


def _mock_session_with_tools(tool_names):
    """Create a mock MCP session that returns given tool names."""
    tools = []
    for name in tool_names:
        t = MagicMock()
        t.name = name
        tools.append(t)
    response = MagicMock()
    response.tools = tools
    session = AsyncMock()
    session.initialize = AsyncMock()
    session.list_tools = AsyncMock(return_value=response)
    return session


def _patch_mcp(session):
    """Context manager to patch stdio_client and ClientSession."""
    import contextlib

    @contextlib.contextmanager
    def ctx():
        with patch('miroflow.tool.manager.stdio_client') as mock_stdio, \\
             patch('miroflow.tool.manager.ClientSession') as mock_cs:
            mock_stdio.return_value.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
            mock_stdio.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=None)
            yield
    return ctx()


def test_find_tool_in_server():
    """Tool present in a server returns that server name."""
    manager = _make_manager(["server_a"])
    session = _mock_session_with_tools(["target_tool", "other_tool"])

    async def run():
        with _patch_mcp(session):
            result = await manager._find_servers_with_tool("target_tool")
        assert isinstance(result, list)
        assert "server_a" in result

    asyncio.run(run())


def test_tool_not_in_any_server():
    """Tool not found in any server returns empty list."""
    manager = _make_manager(["server_a"])
    session = _mock_session_with_tools(["other_tool"])

    async def run():
        with _patch_mcp(session):
            result = await manager._find_servers_with_tool("missing_tool")
        assert isinstance(result, list)
        assert len(result) == 0

    asyncio.run(run())


def test_find_tool_no_servers():
    """No configured servers returns empty list."""
    manager = _make_manager([])

    async def run():
        result = await manager._find_servers_with_tool("any_tool")
        assert isinstance(result, list)
        assert len(result) == 0

    asyncio.run(run())


def test_find_tool_connection_error_handled():
    """Server connection error should be handled gracefully."""
    manager = _make_manager(["bad_server"])
    session = AsyncMock()
    session.initialize = AsyncMock(side_effect=Exception("connection refused"))

    async def run():
        with _patch_mcp(session):
            result = await manager._find_servers_with_tool("tool")
        assert isinstance(result, list)

    asyncio.run(run())
'''

# [8] miroflow-smith-0008: get_all_tool_definitions — simplify
FIXES["MiroMindAI__miroflow-smith-0008"] = '''\
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from mcp import StdioServerParameters
from miroflow.tool.manager import ToolManager


def _make_manager(server_names):
    configs = [
        {"name": name, "params": StdioServerParameters(command="echo", args=[], env={})}
        for name in server_names
    ]
    return ToolManager(server_configs=configs)


def _mock_session_with_tools(tool_defs):
    """Create mock session returning tools with name and inputSchema."""
    tools = []
    for name, schema in tool_defs:
        t = MagicMock()
        t.name = name
        t.description = f"Description of {name}"
        t.inputSchema = schema
        tools.append(t)
    response = MagicMock()
    response.tools = tools
    session = AsyncMock()
    session.initialize = AsyncMock()
    session.list_tools = AsyncMock(return_value=response)
    return session


def test_get_tool_definitions_returns_list():
    """Should return a list with per-server tool definitions."""
    manager = _make_manager(["srv"])
    session = _mock_session_with_tools([
        ("search", {"type": "object", "properties": {"q": {"type": "string"}}}),
        ("read", {"type": "object", "properties": {"path": {"type": "string"}}}),
    ])

    async def run():
        with patch('miroflow.tool.manager.stdio_client') as mock_stdio, \\
             patch('miroflow.tool.manager.ClientSession') as mock_cs:
            mock_stdio.return_value.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
            mock_stdio.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=None)
            result = await manager.get_all_tool_definitions()
        assert isinstance(result, list)
        assert len(result) >= 1
        # Each entry should contain tools info
        assert any("tools" in str(entry) for entry in result)

    asyncio.run(run())


def test_get_tool_definitions_no_servers():
    """No servers configured returns empty list."""
    manager = _make_manager([])

    async def run():
        result = await manager.get_all_tool_definitions()
        assert isinstance(result, list)
        assert len(result) == 0

    asyncio.run(run())


def test_get_tool_definitions_filters_blacklisted():
    """Blacklisted tools should be filtered out."""
    manager = _make_manager(["srv"])
    manager.tool_blacklist = ["blocked_tool"]
    session = _mock_session_with_tools([
        ("good_tool", {"type": "object"}),
        ("blocked_tool", {"type": "object"}),
    ])

    async def run():
        with patch('miroflow.tool.manager.stdio_client') as mock_stdio, \\
             patch('miroflow.tool.manager.ClientSession') as mock_cs:
            mock_stdio.return_value.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
            mock_stdio.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=None)
            result = await manager.get_all_tool_definitions()
        tool_names = [t.get("function", {}).get("name", "") if isinstance(t, dict) else getattr(t, "name", "") for t in result]
        assert "blocked_tool" not in tool_names

    asyncio.run(run())


def test_get_tool_definitions_handles_server_error():
    """Server connection error should not crash, just skip that server."""
    manager = _make_manager(["bad_srv"])
    session = AsyncMock()
    session.initialize = AsyncMock(side_effect=Exception("connect failed"))

    async def run():
        with patch('miroflow.tool.manager.stdio_client') as mock_stdio, \\
             patch('miroflow.tool.manager.ClientSession') as mock_cs:
            mock_stdio.return_value.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
            mock_stdio.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_cs.return_value.__aenter__ = AsyncMock(return_value=session)
            mock_cs.return_value.__aexit__ = AsyncMock(return_value=None)
            result = await manager.get_all_tool_definitions()
        assert isinstance(result, list)

    asyncio.run(run())
'''

# [24] sd-torchtune-smith-0007: load_from_full_model_state_dict — simplify mock classes
FIXES["MiroMindAI__sd-torchtune-smith-0007"] = '''\
import pytest
import torch
import torch.nn as nn
from unittest.mock import patch, MagicMock
from torch.nn.modules.module import _IncompatibleKeys
from torchtune.training._distributed import load_from_full_model_state_dict


class SimpleModel(nn.Module):
    """Simple model for testing state dict loading."""
    def __init__(self):
        super().__init__()
        self.linear = nn.Linear(10, 5)

    def load_state_dict(self, state_dict, strict=True, assign=False):
        return _IncompatibleKeys(missing_keys=[], unexpected_keys=[])


def test_load_returns_incompatible_keys():
    """Function should return _IncompatibleKeys namedtuple."""
    model = SimpleModel()
    full_sd = {"linear.weight": torch.randn(5, 10), "linear.bias": torch.randn(5)}

    with patch('torchtune.training._distributed._DISTRIBUTED_STATE_DICT_API_IS_AVAILABLE', False), \\
         patch('torchtune.training._distributed.distribute_tensor', side_effect=lambda t, *a, **kw: t):
        # Make model.state_dict() return params that look like DTensors (have device_mesh)
        mock_param_w = MagicMock()
        mock_param_w.dtype = torch.float32
        mock_param_w.device_mesh = MagicMock()
        mock_param_w.placements = [MagicMock()]
        mock_param_w._local_tensor = torch.randn(5, 10)

        mock_param_b = MagicMock()
        mock_param_b.dtype = torch.float32
        mock_param_b.device_mesh = MagicMock()
        mock_param_b.placements = [MagicMock()]
        mock_param_b._local_tensor = torch.randn(5)

        with patch.object(model, 'state_dict', return_value={
            "linear.weight": mock_param_w,
            "linear.bias": mock_param_b,
        }):
            result = load_from_full_model_state_dict(
                model=model,
                full_sd=full_sd,
                device=torch.device('cpu'),
                strict=False,
            )
    assert isinstance(result, _IncompatibleKeys)


def test_load_with_plain_tensor_params():
    """Parameters without device_mesh should be loaded as plain tensors."""
    model = SimpleModel()
    full_sd = {"linear.weight": torch.randn(5, 10)}

    # Plain param (no device_mesh attribute)
    plain_param = MagicMock(spec=[\'dtype\'])
    plain_param.dtype = torch.float32

    with patch('torchtune.training._distributed._DISTRIBUTED_STATE_DICT_API_IS_AVAILABLE', False):
        with patch.object(model, 'state_dict', return_value={"linear.weight": plain_param}):
            result = load_from_full_model_state_dict(
                model=model,
                full_sd=full_sd,
                device=torch.device('cpu'),
                strict=False,
            )
    assert isinstance(result, _IncompatibleKeys)


def test_load_with_cpu_offload():
    """CPU offload should move sharded tensors to CPU."""
    model = SimpleModel()
    full_sd = {"linear.weight": torch.randn(5, 10)}

    mock_param = MagicMock()
    mock_param.dtype = torch.float32
    mock_param.device_mesh = MagicMock()
    mock_param.placements = [MagicMock()]
    mock_param._local_tensor = torch.randn(5, 10)

    with patch('torchtune.training._distributed._DISTRIBUTED_STATE_DICT_API_IS_AVAILABLE', False), \\
         patch('torchtune.training._distributed.distribute_tensor', side_effect=lambda t, *a, **kw: t):
        with patch.object(model, 'state_dict', return_value={"linear.weight": mock_param}):
            result = load_from_full_model_state_dict(
                model=model,
                full_sd=full_sd,
                device=torch.device('cpu'),
                cpu_offload=True,
                strict=False,
            )
    assert isinstance(result, _IncompatibleKeys)


def test_load_dtype_conversion():
    """Full tensor should be converted to match sharded param dtype."""
    model = SimpleModel()
    full_sd = {"linear.weight": torch.randn(5, 10, dtype=torch.float64)}

    mock_param = MagicMock()
    mock_param.dtype = torch.float32  # Different from full_sd
    mock_param.device_mesh = MagicMock()
    mock_param.placements = [MagicMock()]
    mock_param._local_tensor = torch.randn(5, 10)

    with patch('torchtune.training._distributed._DISTRIBUTED_STATE_DICT_API_IS_AVAILABLE', False), \\
         patch('torchtune.training._distributed.distribute_tensor') as mock_dist:
        mock_dist.side_effect = lambda t, *a, **kw: t
        with patch.object(model, 'state_dict', return_value={"linear.weight": mock_param}):
            result = load_from_full_model_state_dict(
                model=model,
                full_sd=full_sd,
                device=torch.device('cpu'),
                strict=False,
            )
        # distribute_tensor should have been called with float32 tensor
        called_tensor = mock_dist.call_args[0][0]
        assert called_tensor.dtype == torch.float32
'''

def validate_test_docker(test_source: str, repo: str, docker_image: str = "") -> tuple:
    """Run test in Docker, return (passed, output)."""
    config = DOCKER_CONFIG[repo]
    image_tag = docker_image or config["image_tag"]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", prefix="test_fix_", delete=False) as f:
        f.write(test_source)
        host_path = f.name

    container_path = f"{config['test_dir']}/test_fix_tmp.py"
    try:
        result = subprocess.run(
            [
                "docker", "run", "--rm",
                "-v", f"{host_path}:{container_path}:ro",
                "-w", config["test_dir"],
                image_tag,
                "bash", "-c",
                f"{config['pytest_cmd']} {container_path} -x -v --tb=short --no-header 2>&1",
            ],
            capture_output=True, text=True, timeout=120,
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    finally:
        Path(host_path).unlink(missing_ok=True)


def main():
    # Load instances
    instances = []
    with open(INSTANCES_PATH) as f:
        for line in f:
            if line.strip():
                instances.append(json.loads(line))

    fixed_count = 0
    failed_fixes = []

    for inst_id, new_test in FIXES.items():
        # Find instance
        idx = None
        for i, inst in enumerate(instances):
            if inst["instance_id"] == inst_id:
                idx = i
                break
        if idx is None:
            print(f"SKIP {inst_id}: not found in instances")
            continue

        inst = instances[idx]
        repo = inst["repo"]

        print(f"\n{'='*60}")
        print(f"Testing fix for {inst_id} ({inst['function_metadata']['func_name']})")
        print(f"{'='*60}")

        # Validate in Docker
        ok, output = validate_test_docker(new_test, repo, inst.get("docker_image", ""))

        if ok:
            print(f"  PASS — updating instance")
            # Update test_patch
            from difflib import unified_diff
            test_filename = f"tests/test_smith_{inst['function_metadata']['func_name']}_{idx:04d}.py"
            diff = unified_diff(
                [].copy(),  # empty → new
                new_test.splitlines(keepends=True),
                fromfile=f"a/{test_filename}",
                tofile=f"b/{test_filename}",
            )
            inst["test_patch"] = "".join(diff)

            # Update FAIL_TO_PASS
            test_ids = []
            for line in new_test.splitlines():
                stripped = line.strip()
                if stripped.startswith("def test_"):
                    name = stripped.split("(")[0].replace("def ", "")
                    module = test_filename.replace("/", ".").replace(".py", "")
                    test_ids.append(f"{module}::{name}")
            inst["FAIL_TO_PASS"] = json.dumps(test_ids)

            instances[idx] = inst
            fixed_count += 1
        else:
            print(f"  FAIL — keeping original test")
            print(f"  Output: {output[:500]}")
            failed_fixes.append(inst_id)

    # Write back
    with open(INSTANCES_PATH, "w") as f:
        for inst in instances:
            f.write(json.dumps(inst) + "\n")

    print(f"\n{'='*60}")
    print(f"DONE: {fixed_count}/{len(FIXES)} fixes applied")
    if failed_fixes:
        print(f"Failed: {failed_fixes}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
