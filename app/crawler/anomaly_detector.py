"""
异常价格检测：以 11.17 基准表为参照，过滤明显偏离的价格

判定规则：
1. 找不到基准价 → 放行（宁可漏过，不可错杀）
2. 基准价 < 1000（低价商品，如内存）→ 绝对偏离 > 500 元 或 相对偏离 > 50%
3. 基准价 >= 1000（高价商品，如显卡）→ 相对偏离 > 30%
4. 双向检测：过高（整机/套装/错型号）和过低（二手/假卡/配件）都算异常
5. 未识别出平台时，用该产品各平台价格的区间做兜底判断
"""
import json
from pathlib import Path

from app.core.logger import logger

# 基准价格文件
BENCHMARK_FILE = Path(__file__).resolve().parents[1] / "data" / "benchmark_prices.json"

# 异常阈值
ABS_THRESHOLD = 500        # 低价商品绝对偏离阈值(元)
LOW_PCT_THRESHOLD = 0.50   # 低价商品相对偏离阈值(50%)
PCT_THRESHOLD = 0.30       # 高价商品相对偏离阈值(30%)
LOW_PRICE_BOUNDARY = 1000  # 低/高价商品分界线(元)
# 平台未识别时的容差：允许超出全平台价格区间上下各 20%
RANGE_TOLERANCE = 0.20


def load_benchmark() -> dict:
    """加载全新价基准表，返回 {型号: {...}} """
    data = load_benchmark_meta()
    return data.get("products", {})


def load_used_benchmark() -> dict:
    """加载闲鱼二手成交价基准表，返回 {型号: {...}}

    二手价必须和全新价分开比对：同一型号二手天然便宜几千块，
    如果用全新价基准去校验，正常的二手价会被全判成"偏低异常"。
    """
    data = load_benchmark_meta()
    return data.get("used_products", {})


def load_benchmark_meta() -> dict:
    """加载基准价格表完整内容（含 date / note 等元信息）"""
    if not BENCHMARK_FILE.exists():
        logger.warning(f"[anomaly] 基准价格文件不存在: {BENCHMARK_FILE}")
        return {}
    try:
        with open(BENCHMARK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        date = data.get("date", "未知")
        data_date = data.get("data_date")
        if data_date and data_date != date:
            logger.info(f"[anomaly] 基准生效日期={date}，实际数据日期={data_date}（待替换）")
        else:
            logger.info(f"[anomaly] 基准价格日期: {date}")
        return data
    except Exception as e:
        logger.error(f"[anomaly] 加载基准价格失败: {e}")
        return {}


def save_benchmark_from_prices(prices: list, bench_date: str, source: str = "") -> str:
    """
    用某一期视频提取到的价格回写基准表

    补历史数据时，基准不能拍脑袋填，直接拿区间第一期的真实价格当参照。
    只更新这期视频里出现过的型号和平台；本期没采到的型号保留原值，
    避免整个目录被清空导致后续全部"无基准价"放行。

    :param prices: [{"name", "platform", "price", "condition", "category"}, ...]
    :param bench_date: 基准日期，格式 YYYY-MM-DD
    :param source: 基准来源说明，写进 note
    :return: 基准文件路径
    """
    data = load_benchmark_meta() or {}
    catalog = data.get("products", {})
    used_catalog = data.get("used_products", {})

    captured = {"new": {}, "used": {}}
    for item in prices:
        name = item.get("name")
        price = item.get("price")
        platform = item.get("platform")
        if not name or not price or not platform:
            continue
        key = "used" if item.get("condition") == "used" else "new"
        captured[key].setdefault(name, {})[platform] = price

    # 只改价格，品牌/跑分/史低这些元信息沿用原基准表
    for section, bucket in (("products", captured["new"]),
                            ("used_products", captured["used"])):
        old_section = data.get(section, {})
        for name, platform_prices in bucket.items():
            info = old_section.get(name) or {"category": "gpu"}
            info["prices"] = platform_prices
            info["daily_low"] = min(platform_prices.values())
            old_section[name] = info

    data["date"] = bench_date
    data["data_date"] = bench_date
    data["description"] = (
        f"{bench_date} 行情价格基准表。价格单位：元。"
        "顶层 products 是全新价；used_products 是闲鱼二手成交价。"
        "由当日显卡日报视频截图提取，后续异常检测以这一期为参照。"
    )
    data["note"] = f"基准取自 {bench_date} 的视频{f'（{source}）' if source else ''}。"

    BENCHMARK_FILE.parent.mkdir(parents=True, exist_ok=True)
    BENCHMARK_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        f"[anomaly] 基准表已更新: {bench_date}，"
        f"全新 {len(captured['new'])} 个型号 / 二手 {len(captured['used'])} 个型号"
    )
    return str(BENCHMARK_FILE)


def find_product(product_name: str, benchmark: dict = None) -> tuple:
    """
    在基准表中查找产品，支持模糊匹配

    :return: (标准型号名, 产品基准数据)；未找到返回 (None, None)
    """
    if benchmark is None:
        benchmark = load_benchmark()
    if not product_name:
        return None, None

    name_lower = product_name.lower().replace("super", "").strip()

    # 1. 精确匹配
    if product_name in benchmark:
        return product_name, benchmark[product_name]

    # 2. 忽略大小写/空格匹配
    for std_name, info in benchmark.items():
        if std_name.lower().replace(" ", "") == name_lower.replace(" ", ""):
            return std_name, info

    # 3. 关键词全覆盖匹配：基准型号的所有词都出现在输入里
    best = None
    for std_name, info in benchmark.items():
        keywords = [w for w in std_name.lower().split() if w]
        if keywords and all(kw in product_name.lower() for kw in keywords):
            # 优先匹配更长的型号（RTX 5060 Ti 16G 优于 RTX 5060）
            if best is None or len(std_name) > len(best[0]):
                best = (std_name, info)
    if best:
        return best

    return None, None


def get_benchmark_price(product_name: str, platform: str, benchmark: dict = None):
    """
    获取某型号某平台的基准价

    :return: (基准价, 价格区间 min, max)；完全找不到返回 (None, None, None)
    """
    std_name, info = find_product(product_name, benchmark)
    if not info:
        return None, None, None

    prices = {k: v for k, v in info.get("prices", {}).items() if v}
    if not prices:
        return None, None, None

    lo, hi = min(prices.values()), max(prices.values())
    # 平台价存在则直接用，否则返回 None 表示用区间兜底
    return (prices.get(platform) if prices.get(platform) else None), lo, hi


def _is_out_of_range(price: float, base_price: float) -> bool:
    """判断价格是否偏离基准，低价商品和高价商品规则不同"""
    diff = abs(price - base_price)
    if base_price < LOW_PRICE_BOUNDARY:
        return diff > ABS_THRESHOLD or diff / base_price > LOW_PCT_THRESHOLD
    return diff / base_price > PCT_THRESHOLD


def check_anomaly(product_name: str, platform: str, price: float,
                  benchmark: dict = None, change: float = None) -> tuple:
    """
    检测价格是否异常

    :param change: 视频表格里给出的当日涨跌幅（元），用于识别真实的单日大波动
    :return: (是否异常, 原因说明)
    """
    if not price or price <= 0:
        return True, "价格无效"

    base_price, lo, hi = get_benchmark_price(product_name, platform, benchmark)
    if lo is None:
        return False, "无基准价，放行"

    # 平台未识别 → 用该产品全平台价格区间兜底
    if base_price is None:
        lower_bound = lo * (1 - RANGE_TOLERANCE)
        upper_bound = hi * (1 + RANGE_TOLERANCE)
        if price < lower_bound or price > upper_bound:
            return True, (
                f"价格异常: {price:.0f} 元超出基准区间 "
                f"[{lo:.0f}, {hi:.0f}]（容差±{RANGE_TOLERANCE*100:.0f}%），"
                f"平台未识别({platform})"
            )
        return False, ""

    rule = (
        f"低价商品:绝对偏离>{ABS_THRESHOLD}元 或 相对偏离>{LOW_PCT_THRESHOLD*100:.0f}%"
        if base_price < LOW_PRICE_BOUNDARY
        else f"高价商品相对偏离>{PCT_THRESHOLD*100:.0f}%"
    )

    if not _is_out_of_range(price, base_price):
        return False, ""

    # 基准价只代表基准日，当天真实大涨会被误判（如 RX 9070 京东一天涨 1800 元）。
    # 视频表格自带涨跌幅，用"今价 - 涨跌"还原出昨价再比一次：昨价贴着基准，
    # 说明偏离来自真实波动而不是读错型号/价格，放行。
    if change:
        yesterday = price - change
        if not _is_out_of_range(yesterday, base_price):
            logger.info(
                f"[anomaly] {product_name} @ {platform}: {price:.0f} 元偏离基准 "
                f"{base_price:.0f} 元，扣掉当日涨跌 {change:+.0f} 后为 {yesterday:.0f} 元，"
                f"属正常波动，放行"
            )
            return False, ""

    diff = price - base_price
    abs_diff = abs(diff)
    pct = abs_diff / base_price
    direction = "偏高" if diff > 0 else "偏低"
    reason = (
        f"价格异常({direction}): {price:.0f} 元 vs 基准 {base_price:.0f} 元, "
        f"偏离 {abs_diff:.0f} 元({pct*100:.1f}%), 规则[{rule}]"
    )
    logger.warning(f"[anomaly] {product_name} @ {platform}: {reason}")
    return True, reason


def filter_anomalous_items(items: list, product_name: str, platform: str,
                           benchmark: dict = None) -> list:
    """过滤商品列表中的异常价格，返回正常商品"""
    if benchmark is None:
        benchmark = load_benchmark()

    normal = []
    for item in items:
        price = item.get("price")
        if price is None:
            continue
        is_anomaly, reason = check_anomaly(product_name, platform, price, benchmark)
        if is_anomaly:
            logger.info(f"[anomaly] 排除: {str(item.get('name', ''))[:30]} 价格 {price} ({reason})")
            continue
        normal.append(item)
    return normal
