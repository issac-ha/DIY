"""
视频旁白结构化：把 ASR 转写拆成「当日价格归纳」和「硬件新闻」两块

两块内容回答的问题不同：
- 价格归纳：今天整体涨跌、哪些型号被点名、给了什么建议
- 硬件新闻：行业动态（新品、砍单、政策、人事），跟具体型号价格无关

这两块要入库供以后查询，所以必须输出成 JSON，不能是一段散文。
"""
import json
import re

from app.core.logger import logger
from app.llm_client import get_llm

DIGEST_PROMPT = """把这段硬件日报视频的旁白拆成两部分，输出 JSON，不要输出其他内容。

视频标题：{title}
旁白：
{transcript}

输出格式：
{{
  "market_summary": "当日价格归纳，200 字以内",
  "news": [{{"title": "新闻标题，20 字以内", "detail": "两到三句，保留关键数字和主体"}}]
}}

规则：
1. market_summary 只写旁白里说到的当日行情：整体涨跌、内存/闪存/显卡等板块表现、
   被点名的型号和价格。旁白没说的不要补，不要复述新闻
2. news 只收硬件行业动态：新品发布、规格标准、厂商动作、供应链砍单、人事变动。
   价格涨跌本身不算新闻，不要写进 news
3. 新闻条目按旁白顺序排列，最多 6 条；没有新闻就给空数组
4. 保留原文的具体数字，不要自己换算或估算
5. 旁白有识别错误（如"因为达"应为"英伟达"），按上下文纠正成正确名称
"""


def extract_digest(title: str, transcript: str) -> dict:
    """
    从旁白提取价格归纳与硬件新闻

    :return: {"market_summary": str, "news": [{"title": str, "detail": str}]}
    """
    empty = {"market_summary": "", "news": []}
    if not transcript:
        return empty

    try:
        resp = get_llm().invoke(DIGEST_PROMPT.format(title=title, transcript=transcript[:6000]))
        data = _parse_json(resp.content)
    except Exception as e:
        logger.error(f"[digest] 提取失败: {e}")
        return empty

    news = []
    for item in data.get("news", []):
        if isinstance(item, dict) and item.get("title"):
            news.append({
                "title": str(item["title"]).strip(),
                "detail": str(item.get("detail", "")).strip(),
            })
    return {
        "market_summary": str(data.get("market_summary", "")).strip(),
        "news": news,
    }


def _parse_json(text: str) -> dict:
    """模型偶尔会用 ```json 包裹或加前后说明，这里只取第一个 JSON 对象"""
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        logger.error(f"[digest] JSON 解析失败: {e}")
        return {}
    return data if isinstance(data, dict) else {}
