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

# 单次请求最多几张截图。
# 4 张时一批可能同时出现内存、CPU、闲鱼三张表，输出超过字数上限后模型会截断，
# 尾部那张表整段丢失（11-20 那期闲鱼只入库 12 条）。降到 2 张，一批最多两张表。
BATCH_SIZE = 2

# 单批输出的字数上限。超过 95% 视为可能截断，触发逐帧重读
MAX_OUTPUT_CHARS = 2500


class VisionServiceError(RuntimeError):
    """视觉模型服务不可用：欠费、鉴权失败、限流等

    这类错误和「这一期视频没有价格表」性质完全不同。前者重试多少次都不会成功，
    把它当成数据缺口记录下来会让日期被永久跳过，所以必须向上抛出并中止采集。
    """


# 命中这些关键词说明是账号或配额问题，不是单次请求的偶发失败
_SERVICE_UNAVAILABLE_KEYWORDS = (
    "overdue balance",
    "insufficient balance",
    "quota",
    "invalid api key",
    "incorrect api key",
    "unauthorized",
    "authentication",
)


def _is_service_unavailable(err: Exception) -> bool:
    """判断异常是否属于服务级不可用，而不是可重试的单次失败"""
    status = getattr(err, "status_code", None)
    if status is None:
        response = getattr(err, "response", None)
        status = getattr(response, "status_code", None)
    if status in (401, 402, 403, 429):
        return True

    message = str(err).lower()
    return any(keyword in message for keyword in _SERVICE_UNAVAILABLE_KEYWORDS)


# 单批截图的分析 prompt
FRAME_PROMPT = """这些是同一个硬件资讯视频的连续截图（按时间顺序）。

如果画面里有价格表格，按表格的每一行输出一行，格式：
行号|型号|平台=价格/涨跌幅|平台=价格/涨跌幅|...

示例：
01|RTX 5090 D|taobao=44499/-|pdd=42999/+300|douyin=52000/-
02|DDR5 32GB*2|taobao=4844/+245|pdd=5399/+100|douyin=5299/+101
03|RTX 5090D 32G|xianyu=32200/-125

规则：
1. 一行只能有一个型号，该型号的所有平台写在同一行，禁止把同一型号拆成多行
2. 行号从 01 开始连续递增，表格有几行就输出几行，禁止合并相邻行或跳过整行
3. 格式是 平台=价格/涨跌幅：涨跌幅照抄，负号保留；没有涨跌幅写 -；
   无货写"无货"；看不清写"?"，不要猜
4. 平台映射：某东/京东/自营 → jd；某宝/淘宝/天猫 → taobao；
   某多多/拼多多 → pdd；某Yin/抖音 → douyin；闲鱼/小黄鱼 → xianyu
5. 显卡型号完整照抄，末尾的 S / Ti / Super / D / 12G / 16G 不能省略：
   RTX 4070、RTX 4070 S、RTX 4070 Ti S 是三张不同的卡，
   RTX 4080 和 RTX 4080 S 也是两张不同的卡，漏掉 S 会导致两行价格串号
6. 内存、硬盘表的平台列是"现价 / 涨跌幅"：价格取现价，涨跌幅取后面的数字；
   型号写 DDR5 32GB*2、PCIe 4.0 1TB 这类
7. 标题含"小黄鱼"或"闲鱼"的表是二手成交价：成交价当价格，平台填 xianyu，
   TSE跑分、性价比不是价格也不是涨跌幅
8. 天梯图、跑分榜只有分数没有价格，跳过
9. 没有价格信息时只回"无价格数据"
10. 不要描述画面风格、标题、博主，只输出数据行
11. 总输出不超过 2500 字
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
        max_tokens=MAX_OUTPUT_CHARS,
        # doubao-seed-2-1 默认开启深度思考，会把 max_tokens 全耗在 reasoning 上，
        # 正文返回空。读表格只需要照抄，关掉思考。
        extra_body={"thinking": {"type": "disabled"}},
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

    def _run(batch: list) -> tuple:
        """读一批截图，返回（时间标签, 模型输出）"""
        content = [{"type": "text", "text": FRAME_PROMPT}]
        for snap in batch:
            content.append({
                "type": "image_url",
                "image_url": {"url": _to_data_url(snap["bytes"])},
            })
        resp = llm.invoke([HumanMessage(content=content)])
        times = ", ".join(str(s["time"]) for s in batch)
        return times, resp.content.strip()

    for start in range(0, len(snapshots), BATCH_SIZE):
        batch = snapshots[start:start + BATCH_SIZE]
        batch_no = start // BATCH_SIZE + 1
        try:
            times, desc = _run(batch)
            # 一批里可能同时出现内存、CPU、闲鱼三张表，写到字数上限时模型直接截断，
            # 尾部那张表整段消失。发现截断就逐帧重读，单帧一张表不会再超限。
            if len(batch) > 1 and (
                desc.endswith("...") or len(desc) >= MAX_OUTPUT_CHARS * 0.95
            ):
                logger.warning(f"[vision] 第 {batch_no} 批输出疑似截断，改为逐帧重读")
                parts = []
                for snap in batch:
                    snap_times, snap_desc = _run([snap])
                    parts.append(f"[截图时间 {snap_times} 秒]\n{snap_desc}")
                descriptions.append("\n\n".join(parts))
            else:
                descriptions.append(f"[截图时间 {times} 秒]\n{desc}")
            logger.info(f"[vision] 分析第 {batch_no} 批截图完成")
        except Exception as e:
            # 欠费、密钥失效这类问题继续跑没有意义，直接中止整轮采集
            if _is_service_unavailable(e):
                raise VisionServiceError(
                    f"视觉模型不可用（账号欠费或配额不足），采集中止: {e}"
                ) from e
            logger.error(f"[vision] 分析截图失败: {e}")
            times = ", ".join(str(s["time"]) for s in batch)
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
            if _is_service_unavailable(e):
                raise VisionServiceError(
                    f"视觉模型不可用（账号欠费或配额不足），采集中止: {e}"
                ) from e
            logger.error(f"[vision] 生成总结失败: {e}")
            summary = f"总结生成失败: {e}"

    return {
        "title": title,
        "visual": visual,
        "summary": summary,
    }
