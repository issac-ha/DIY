"""
B站视频价格提取器：用LLM从视频字幕/简介/评论中提取硬件价格

输入：视频的完整文本（标题+简介+字幕+评论）
输出：[{name, category, platform, price}, ...] 结构化价格数据
"""
import json
import re

from app.llm_client import get_llm
from app.core.logger import logger

# LLM 提取 prompt：明确要求只提取有明确数字价格的硬件
EXTRACT_PROMPT = """你是一个硬件价格数据提取助手。请从下面的视频文本中提取显卡、内存、硬盘的价格信息。

文本里的每条价格是"型号 | 平台 | 价格 | 涨跌幅"四段格式。

要求：
1. 只提取有明确数字价格的型号，不要猜测或推算价格
2. 型号必须从下面的目录中原样选择，不得改写、不得补充后缀
3. category 只能是：gpu（显卡）、memory（内存）、ssd（硬盘）、cpu（处理器）
4. platform 只能是：jd（京东）、taobao（淘宝）、pdd（拼多多）、douyin（抖音）、xianyu（闲鱼/小黄鱼）
5. change 是表格里的当日涨跌幅（元），负号表示下跌，原样照抄；空白或没有就填 null
6. 如果同一个型号在多个平台有价格，分别列出
7. 闲鱼/小黄鱼表是二手成交价，平台填 xianyu
8. 天梯图、跑分榜里只有分数没有价格，不要把分数当成价格
9. 如果文本中没有明确价格，返回空列表
10. 输入可能是"行号|型号|平台=价格/涨跌幅|平台=价格/涨跌幅"的形式，
    同一行的多个平台要拆成多条记录，型号保持一致；价格为"无货"或"?"的跳过

型号目录（只能输出目录里已有的名字）：
全新：__NEW_NAMES__
二手：__USED_NAMES__
画面写 "4080 S" 就输出目录里的 "RTX 4080 S"，不要改写成 Super；
目录里没有的型号直接忽略，不要新建名字。

返回JSON格式，不要有其他文字：
{"prices": [{"name": "型号", "category": "品类", "platform": "平台", "price": 价格数字, "change": 涨跌幅数字或null}]}

视频文本：
\"\"\"__TEXT__\"\"\"
"""


def extract_prices(text: str, new_names: list = None, used_names: list = None) -> list:
    """
    从视频文本中提取硬件价格（显卡/内存/硬盘）

    :param text: 视频完整文本（标题+简介+字幕+评论）
    :param new_names: 全新型号目录，传给模型做严格取名
    :param used_names: 二手型号目录
    :return: [{name, category, platform, price, change, condition}, ...]
    """
    if not text or len(text.strip()) < 50:
        logger.info("[extract] 文本过短，跳过")
        return []

    llm = get_llm()
    # 用 replace 而不是 format，避免 JSON 示例里的大括号被当成占位符
    prompt = EXTRACT_PROMPT.replace("__TEXT__", text[:8000])
    prompt = prompt.replace("__NEW_NAMES__", "、".join(new_names or []) or "无")
    prompt = prompt.replace("__USED_NAMES__", "、".join(used_names or []) or "无")

    try:
        response = llm.invoke(prompt)
        content = response.content.strip()
        # 提取 JSON（LLM 可能返回 markdown 代码块）
        prices = _parse_json(content)
        prices = _clean_prices(prices)
        logger.info(f"[extract] 提取到 {len(prices)} 条价格数据")
        return prices
    except Exception as e:
        logger.error(f"[extract] LLM提取失败: {e}")
        return []


VALID_PLATFORMS = {"jd", "taobao", "pdd", "douyin", "xianyu"}
VALID_CATEGORIES = {"gpu", "memory", "ssd", "cpu"}

# 模型可能输出中文平台名，统一映射到内部代号
PLATFORM_ALIASES = {
    "京东": "jd", "某东": "jd", "自营": "jd", "jingdong": "jd",
    "淘宝": "taobao", "某宝": "taobao", "天猫": "taobao", "tmall": "taobao",
    "拼多多": "pdd", "某多多": "pdd", "pinduoduo": "pdd",
    "抖音": "douyin", "某音": "douyin",
    "闲鱼": "xianyu", "某鱼": "xianyu", "小黄鱼": "xianyu", "xy": "xianyu",
}


def _infer_condition(platform: str) -> str:
    """闲鱼卖的是二手成交价，其余渠道按全新处理"""
    return "used" if platform == "xianyu" else "new"


def _infer_category(name: str) -> str:
    """
    按型号名推断品类，模型没给或给错时兜底

    DDR 开头是内存，PCIe/NVMe 是硬盘，Intel i/U 系列和 AMD R 系列是 CPU，
    其余（RTX/RX/ARC）按显卡处理。
    """
    upper = name.upper()
    if "DDR" in upper or "内存" in name:
        return "memory"
    if "PCIE" in upper or "NVME" in upper or "SSD" in upper or "硬盘" in name:
        return "ssd"
    # 注意 RX 9070 这类显卡不能命中 R7-，所以必须带连字符
    if re.match(r"^(I[3579]|R[3579]|U[3579])-", upper):
        return "cpu"
    return "gpu"


def _normalize_name(name: str) -> str:
    """
    型号名归一化，保证同一张卡不会因为写法差异被存成两条产品

    典型差异：画面写 RTX 5090 / D，基准表写 RTX 5090 D；
    画面写 RTX 5070 TI，基准表写 RTX 5070 Ti。
    """
    # 只处理两侧带空格的斜杠（RTX 5090 / D）。
    # CPU 型号本身含斜杠（U7-265K/KF），去掉会把目录名改坏
    name = re.sub(r"\s+/\s*", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"\bTI\b", "Ti", name, flags=re.IGNORECASE)
    # 目录里统一用 S 结尾（画面也写 4080 S），模型偶尔展开成 Super，收敛回 S
    name = re.sub(r"\bSUPER\b", "S", name, flags=re.IGNORECASE)
    # 画面常写 16G / 8G，基准表统一用 GB，不加 B 会被当成两个型号
    name = re.sub(r"\b(\d{1,2})G\b", r"\1GB", name)
    return name


def _parse_change(value):
    """把模型返回的涨跌幅转成数字；空白、横杠、null 都返回 None"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-", "—", "null", "None", "N/A"}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _clean_prices(prices: list) -> list:
    """
    清洗抽取结果

    平台必须能映射到四个合法渠道，价格必须为正数。
    天梯图、跑分榜这类画面没有平台列，模型会把图里的数字误当成价格，
    平台落成 unknown 的一律丢弃，避免污染价格库。
    """
    cleaned = []
    for item in prices:
        if not isinstance(item, dict):
            continue
        name = _normalize_name(str(item.get("name", "")))
        platform = str(item.get("platform", "")).strip().lower()
        platform = PLATFORM_ALIASES.get(platform, platform)
        category = str(item.get("category", "")).strip().lower()
        try:
            price = float(item.get("price"))
        except (TypeError, ValueError):
            continue
        if not name or platform not in VALID_PLATFORMS or price <= 0:
            continue
        if category not in VALID_CATEGORIES:
            category = _infer_category(name)
        cleaned.append({"name": name, "category": category,
                        "platform": platform, "price": price,
                        "change": _parse_change(item.get("change")),
                        "condition": _infer_condition(platform)})
    return cleaned


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
