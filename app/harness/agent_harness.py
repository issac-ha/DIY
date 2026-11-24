"""Agent 统一运行时外壳

模型本身只是一次 HTTP 请求,真正让它能"自己查数据再回答"的是一层循环:
调用模型 -> 模型决定调工具 -> 执行工具 -> 结果塞回上下文 -> 再调用模型。
LangChain 的 create_agent 负责这个循环,这里负责循环外面的事:

- 超时:同步 invoke 没有原生超时,用线程池等待,超时后放弃本次结果
- 重试:只对超时、连接失败、限流这类可恢复错误退避重试,业务异常不重试
- 可观测:记录轮数、工具调用链、耗时、token,一行日志看出这轮干了什么
- 降级:基础设施故障抛 LLMUnavailableError,接口层转 503,而不是裸 500

没有把采集管线并进来:采集有自己的错误分类(视觉服务故障不计缺口、网络失败只告警),
语义不同,套通用重试会破坏已有判定。
"""
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

from app.core.logger import logger
from app.harness.types import AgentResult, RunStats, ToolCallRecord


class LLMUnavailableError(RuntimeError):
    """模型服务不可用:超时、连不上、限流。接口层应转成 503"""


class _RetryableError(Exception):
    """内部标记,用于把可恢复错误和业务异常区分开"""


# 异常信息里出现这些关键字,说明是网络或服务侧问题,重试有意义
_RETRYABLE_HINTS = (
    "timeout", "timed out", "connection", "connect",
    "rate limit", "429", "500", "502", "503", "504", "overloaded",
)


def _is_retryable(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(hint in text for hint in _RETRYABLE_HINTS)


class AgentHarness:
    """把 agent 的构建和调用收口到一处

    build_agent 传可调用对象而不是实例:LLM 客户端和工具到第一次真正对话时才初始化,
    导入模块、跑单元测试不会碰外部依赖。
    """

    def __init__(self, build_agent, timeout: float = 90.0,
                 max_retries: int = 1, backoff: float = 2.0):
        self._build_agent = build_agent
        self._agent = None
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff = backoff

    def _get_agent(self):
        if self._agent is None:
            self._agent = self._build_agent()
        return self._agent

    def run(self, history: list[dict], session_id: str = "-") -> AgentResult:
        """跑一轮对话

        成功返回 AgentResult;基础设施故障重试后仍失败,抛 LLMUnavailableError;
        业务异常(比如工具代码有 bug)不重试,原样抛出保留调用栈。
        """
        start = time.perf_counter()
        stats = RunStats()
        last_error = ""

        for attempt in range(self._max_retries + 1):
            try:
                raw = self._invoke_with_timeout(history)
            except _RetryableError as exc:
                last_error = str(exc)
                stats.retries += 1
                if attempt < self._max_retries:
                    # 指数退避:服务侧限流时连续重试只会加重问题
                    wait = self._backoff * (2 ** attempt)
                    logger.warning(
                        f"[agent] session={session_id} 第 {attempt + 1} 次调用失败,"
                        f"{wait:.0f}s 后重试: {exc}"
                    )
                    time.sleep(wait)
                    continue
                break

            stats.elapsed_ms = int((time.perf_counter() - start) * 1000)
            self._collect_stats(raw, stats)
            logger.info(
                f"[agent] session={session_id} steps={stats.steps} "
                f"tools={stats.tool_chain()} elapsed={stats.elapsed_ms}ms "
                f"tokens={stats.input_tokens}/{stats.output_tokens} retries={stats.retries}"
            )
            return AgentResult(reply=self._extract_reply(raw), stats=stats)

        logger.error(
            f"[agent] session={session_id} 模型不可用,已重试 {stats.retries} 次: {last_error}"
        )
        raise LLMUnavailableError(last_error)

    def _invoke_with_timeout(self, history: list[dict]):
        """带超时的单次调用

        同步 invoke 无法被强行中断,超时只能"放弃等待",后台线程会随 HTTP 超时自行结束。
        所以线程池用 shutdown(wait=False),否则超时后仍会阻塞在等待线程上,超时形同虚设。
        """
        agent = self._get_agent()
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(agent.invoke, {"messages": history})
        try:
            return future.result(timeout=self._timeout)
        except FuturesTimeout:
            raise _RetryableError(f"模型响应超时({self._timeout:.0f}s)") from None
        except Exception as exc:
            if _is_retryable(exc):
                raise _RetryableError(f"{type(exc).__name__}: {exc}") from exc
            raise
        finally:
            pool.shutdown(wait=False)

    @staticmethod
    def _extract_reply(raw: dict) -> str:
        messages = raw.get("messages") or []
        if not messages:
            raise LLMUnavailableError("模型返回空消息列表")
        return str(getattr(messages[-1], "content", "") or "")

    @staticmethod
    def _collect_stats(raw: dict, stats: RunStats) -> None:
        """从消息序列里还原轮数、工具链和 token 用量

        AIMessage.tool_calls 是"打算调什么",ToolMessage 是"调完的结果",
        两者按出现顺序一一对应,所以用队列配对。
        """
        pending: list[ToolCallRecord] = []
        for message in raw.get("messages") or []:
            kind = getattr(message, "type", "")
            if kind == "ai":
                stats.steps += 1
                usage = getattr(message, "usage_metadata", None) or {}
                stats.input_tokens += usage.get("input_tokens", 0) or 0
                stats.output_tokens += usage.get("output_tokens", 0) or 0
                for call in getattr(message, "tool_calls", None) or []:
                    pending.append(ToolCallRecord(
                        name=call.get("name", ""),
                        args=call.get("args", {}) or {},
                    ))
            elif kind == "tool":
                if pending:
                    record = pending.pop(0)
                else:
                    record = ToolCallRecord(name=getattr(message, "name", ""))
                # 工具抛异常时框架会转成 status=error 的消息,不当成整轮失败
                record.ok = getattr(message, "status", "success") != "error"
                if not record.ok:
                    record.error = str(getattr(message, "content", ""))[:200]
                stats.tool_calls.append(record)
