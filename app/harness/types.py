"""Agent 运行时外壳(harness)用到的数据结构

harness 只做两件事:驱动模型跑完工具循环,然后把这一趟发生了什么记下来。
结果和统计分开定义,接口层只关心 reply,排查问题时再取 stats。
"""
from dataclasses import dataclass, field


@dataclass
class ToolCallRecord:
    """一次工具调用的记录"""

    name: str
    args: dict = field(default_factory=dict)
    ok: bool = True
    error: str = ""


@dataclass
class RunStats:
    """一次运行的统计:模型轮数、工具链、耗时、token"""

    elapsed_ms: int = 0
    retries: int = 0
    steps: int = 0
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0

    def tool_chain(self) -> str:
        """把工具调用串成一行,日志里能直接看出这轮查了什么"""
        if not self.tool_calls:
            return "-"
        return " -> ".join(call.name for call in self.tool_calls)


@dataclass
class AgentResult:
    """对外结果:回答正文 + 本轮统计"""

    reply: str
    stats: RunStats = field(default_factory=RunStats)
