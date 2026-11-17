"""
B站视频价格提取器：用LLM从视频字幕/简介/评论中提取显卡价格

输入：视频的完整文本（标题+简介+字幕+评论）
输出：[{name, platform, price}, ...] 结构化价格数据
"""
import json
import re

from app.llm_client import get_llm
from app.core.logger import logger

# LLM 提取 prompt：明确要求只提取有明确数字价格的显卡
EXTRACT_PROMPT = """你是一个硬件价格数据提取助手。请从下面的视频文本中提取显卡（GPU）的价格信息。

要求：
1. 只提取有明确数字价格的显卡，不要猜测或推算价格
2. 显卡型号要标准化，例如 "RTX 4060"、"RX 7800 XT"、"RTX 5070 Ti"
3. 平台字段只能是：jd（京东）、taobao（淘宝）、pdd（拼多多）、douyin（抖音）、unknown（未知）
4. 如果同一个显卡在多个平台有价格，分别列出
5. 如果文本中没有明确的显卡价格，返回空列表
6. 忽略内存、CPU、主板等其他硬件，只提取显卡

返回JSON格式，不要有其他文字：
{"prices": [{"name": "显卡型号", "platform": "平台", "price": 价格数字}]}

视频文本：
\"\"\"__TEXT__\"\"\"
"""


def extract_prices(text: str) -> list:
    """
    从视频文本中提取显卡价格

    :param text: 视频完整文本（标题+简介+字幕+评论）
    :return: [{name, platform, price}, ...]
    """
    if not text or len(text.strip()) < 50:
        logger.info("[extract] 文本过短，跳过")
        return []

    llm = get_llm()
    # 用 replace 而不是 format，避免 JSON 示例里的大括号被当成占位符
    prompt = EXTRACT_PROMPT.replace("__TEXT__", text[:8000])

    try:
        response = llm.invoke(prompt)
        content = response.content.strip()
        # 提取 JSON（LLM 可能返回 markdown 代码块）
        prices = _parse_json(content)
        logger.info(f"[extract] 提取到 {len(prices)} 条价格数据")
        return prices
    except Exception as e:
        logger.error(f"[extract] LLM提取失败: {e}")
        return []


def _parse_json(content: str) -> list:
    """从 LLM 返回内容中解析 JSON"""
    # 去掉 markdown 代码块标记
    content = re.sub(r"```json\s*", "", content)
    content = re.sub(r"```\s*$", "", content)
    content = content.strip()

    try:
        data = json.loads(content)
        if isinstance(data, dict) and "prices" in data:
            return data["prices"]
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    # 尝试用正则提取 JSON 数组
    match = re.search(r"\{[\s\S]*\"prices\"[\s\S]*\}", content)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data, dict) and "prices" in data:
                return data["prices"]
        except json.JSONDecodeError:
            pass

    logger.warning(f"[extract] JSON解析失败，原始内容: {content[:200]}")
    return []
