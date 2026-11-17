"""
视频视觉总结：把视频截图交给视觉模型分析，结合字幕输出视频总结

流程：
1. 截图分批（每批最多 6 张，避免单次请求过大）
2. 视觉模型描述每批截图的内容（识别价格表、型号、价格数字）
3. 汇总所有批次描述 + 字幕，生成最终视频总结
"""
import base64

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from app.config import VISION_API_KEY, VISION_BASE_URL, VISION_MODEL
from app.core.logger import logger

# 单次请求最多几张截图
BATCH_SIZE = 4

# 单批截图的分析 prompt
FRAME_PROMPT = """这些是同一个硬件资讯视频的连续截图（按时间顺序）。

如果画面里有价格表格或价格文字，逐行提取能看清的内容，一行一条：
型号 | 平台 | 价格

规则：
1. 表格里的数字清晰就照抄，不要换算、不要推测
2. 模糊到无法确认的单元格直接跳过，不要猜
3. 平台按符号映射到标准名：
   - 某东 / 东 / 狗东 / 京东自营 → jd
   - 某宝 / 宝 / 淘宝 → taobao
   - 某多多 / 夕 / 多多 → pdd
   - 某Yin / 音 / 抖 / 抖音 → douyin
   - 无法判断 → unknown
4. 没有价格信息时只回"无价格数据"
5. 不要描述画面风格、标题、博主，只输出数据行
6. 总输出不超过 250 字
"""

# 最终总结 prompt
SUMMARY_PROMPT = """根据下面的截图识别结果和字幕，写一份视频总结。

格式（简洁，总共不超过 300 字）：
1. 主题：一句话
2. 价格：只列有明确数字的型号和价格
3. 涨跌：哪些涨、哪些跌
4. 结论：视频给出的建议

只基于给定材料，缺失就写"未提及"。

截图识别结果：
{visual}

字幕：
{subtitle}
"""


def _vision_llm() -> ChatOpenAI:
    """获取视觉模型客户端"""
    if not VISION_API_KEY:
        raise RuntimeError(
            "未配置 VISION_API_KEY，无法分析截图。请在 .env 中填写视觉模型配置。"
        )
    return ChatOpenAI(
        api_key=VISION_API_KEY,
        base_url=VISION_BASE_URL,
        model=VISION_MODEL,
        temperature=0.1,
        max_tokens=800,
    )


def _to_data_url(jpeg_bytes: bytes) -> str:
    """把图片字节转成 data URL"""
    b64 = base64.b64encode(jpeg_bytes).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"


def analyze_snapshots(snapshots: list) -> str:
    """
    分批让视觉模型分析截图

    :param snapshots: [{"time": 秒, "bytes": 图片字节}, ...]
    :return: 所有批次的描述拼接文本
    """
    if not snapshots:
        return ""

    llm = _vision_llm()
    descriptions = []

    for start in range(0, len(snapshots), BATCH_SIZE):
        batch = snapshots[start:start + BATCH_SIZE]
        content = [{"type": "text", "text": FRAME_PROMPT}]
        for snap in batch:
            content.append({
                "type": "image_url",
                "image_url": {"url": _to_data_url(snap["bytes"])},
            })

        times = ", ".join(str(s["time"]) for s in batch)
        try:
            resp = llm.invoke([HumanMessage(content=content)])
            desc = resp.content.strip()
            descriptions.append(f"[截图时间 {times} 秒]\n{desc}")
            logger.info(f"[vision] 分析第 {start // BATCH_SIZE + 1} 批截图完成")
        except Exception as e:
            logger.error(f"[vision] 分析截图失败: {e}")
            descriptions.append(f"[截图时间 {times} 秒] 分析失败: {e}")

    return "\n\n".join(descriptions)


def summarize_video(title: str, subtitle: str, snapshots: list) -> dict:
    """
    生成视频总结（截图 + 字幕）

    :param title: 视频标题
    :param subtitle: 字幕/简介/评论文本
    :param snapshots: 截图列表
    :return: {"visual": 截图分析, "summary": 最终总结}
    """
    # 1. 视觉模型分析截图
    visual = analyze_snapshots(snapshots)

    # 2. 结合字幕生成最终总结
    summary = ""
    if visual or subtitle:
        try:
            llm = _vision_llm()
            prompt = SUMMARY_PROMPT.format(
                visual=visual or "无截图分析",
                subtitle=subtitle[:6000] if subtitle else "无字幕",
            )
            resp = llm.invoke(prompt)
            summary = resp.content.strip()
        except Exception as e:
            logger.error(f"[vision] 生成总结失败: {e}")
            summary = f"总结生成失败: {e}"

    return {
        "title": title,
        "visual": visual,
        "summary": summary,
    }
