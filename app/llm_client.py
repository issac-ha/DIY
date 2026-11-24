"""LLM客户端 : 封装 Deepseek API 调用"""
from langchain_openai import ChatOpenAI
from app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

def get_llm():
    """获取 LLM 客户端"""
    return ChatOpenAI(
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        model=LLM_MODEL,
        temperature=0.3,
        # 单次 HTTP 请求超时。harness 超时后放弃等待,这个值保证后台请求能自己结束,
        # 不会一直占着连接
        timeout=60,
        # 关掉 SDK 自带重试:重试策略统一由 harness 控制,
        # 两边都开会让一次失败放大成多次调用,还会打乱退避节奏
        max_retries=0,
    )
