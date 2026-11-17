"""LLM客户端 : 封装 Deepseek API 调用"""
from langchain_openai import ChatOpenAI
from app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

def get_llm():
    """获取 LLM 客户端"""
    return ChatOpenAI(
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        model=LLM_MODEL,
        temperature=0.3
    )