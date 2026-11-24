"""AgentHarness 单元测试

不连真实模型:用形状和 LangChain 消息一致假对象,只验证外壳自己的行为
——回答提取、统计还原、超时重试、业务异常不重试。
"""
import time
from types import SimpleNamespace

import pytest

from app.harness import AgentHarness, LLMUnavailableError


def _msg(kind, **kwargs):
    """构造假的 LangChain 消息:只带 harness 会读的字段"""
    return SimpleNamespace(type=kind, **kwargs)


class FakeAgent:
    """可控的假 agent:能指定返回消息、延迟和抛出的异常"""

    def __init__(self, messages=None, delay=0.0, error=None):
        self._messages = messages or []
        self._delay = delay
        self._error = error
        self.calls = 0

    def invoke(self, payload):
        self.calls += 1
        if self._delay:
            time.sleep(self._delay)
        if self._error:
            raise self._error
        return {"messages": self._messages}


def _ask():
    return [{"role": "user", "content": "RTX 5070 现在多少钱"}]


def test_run_extracts_reply_and_tracks_tool_calls():
    """正常一轮:两次模型调用、一次工具调用,回复和统计都要对"""
    first_ai = _msg(
        "ai",
        content="",
        tool_calls=[{"name": "query_database", "args": {"sql": "select 1"}}],
        usage_metadata={"input_tokens": 100, "output_tokens": 20},
    )
    tool_msg = _msg("tool", name="query_database", content="6899", status="success")
    final_ai = _msg(
        "ai",
        content="最低 6899 元",
        tool_calls=[],
        usage_metadata={"input_tokens": 150, "output_tokens": 30},
    )

    harness = AgentHarness(lambda: FakeAgent([first_ai, tool_msg, final_ai]))
    result = harness.run(_ask())

    assert result.reply == "最低 6899 元"
    assert result.stats.steps == 2
    assert result.stats.tool_chain() == "query_database"
    assert result.stats.tool_calls[0].ok is True
    assert result.stats.tool_calls[0].args == {"sql": "select 1"}
    assert result.stats.input_tokens == 250
    assert result.stats.output_tokens == 50


def test_tool_error_is_recorded_not_raised():
    """工具内部报错由框架转成消息,不该让整轮对话失败"""
    ai_call = _msg("ai", content="", tool_calls=[{"name": "query_database", "args": {}}])
    tool_error = _msg("tool", name="query_database", content="语法错误", status="error")
    final_ai = _msg("ai", content="查询失败了", tool_calls=[])

    harness = AgentHarness(lambda: FakeAgent([ai_call, tool_error, final_ai]))
    result = harness.run(_ask())

    assert result.stats.tool_calls[0].ok is False
    assert "语法错误" in result.stats.tool_calls[0].error


def test_timeout_retries_then_raises_unavailable():
    """超时属于可重试:重试次数用完后抛 LLMUnavailableError"""
    fake = FakeAgent(delay=0.3)
    harness = AgentHarness(lambda: fake, timeout=0.1, max_retries=1, backoff=0)

    with pytest.raises(LLMUnavailableError):
        harness.run(_ask())

    assert fake.calls == 2  # 首次 + 一次重试


def test_connection_error_is_retried():
    """连接类错误同样按可恢复处理"""
    fake = FakeAgent(error=ConnectionError("connection reset by peer"))
    harness = AgentHarness(lambda: fake, max_retries=1, backoff=0)

    with pytest.raises(LLMUnavailableError):
        harness.run(_ask())

    assert fake.calls == 2


def test_business_error_is_not_retried():
    """业务异常重试也不会好,原样抛出,保留调用栈方便定位"""
    fake = FakeAgent(error=ValueError("工具参数拼错了"))
    harness = AgentHarness(lambda: fake, max_retries=2, backoff=0)

    with pytest.raises(ValueError):
        harness.run(_ask())

    assert fake.calls == 1
