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
    """加载基准价格表，返回 {型号: {...}} """
    data = load_benchmark_meta()
    return data.get("products", {})


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


def check_anomaly(product_name: str, platform: str, price: float,
                  benchmark: dict = None) -> tuple:
    """
    检测价格是否异常

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

    diff = price - base_price
    abs_diff = abs(diff)
    pct = abs_diff / base_price

    if base_price < LOW_PRICE_BOUNDARY:
        is_anomaly = abs_diff > ABS_THRESHOLD or pct > LOW_PCT_THRESHOLD
        rule = f"低价商品:绝对偏离>{ABS_THRESHOLD}元 或 相对偏离>{LOW_PCT_THRESHOLD*100:.0f}%"
    else:
        is_anomaly = pct > PCT_THRESHOLD
        rule = f"高价商品相对偏离>{PCT_THRESHOLD*100:.0f}%"

    if is_anomaly:
        direction = "偏高" if diff > 0 else "偏低"
        reason = (
            f"价格异常({direction}): {price:.0f} 元 vs 基准 {base_price:.0f} 元, "
            f"偏离 {abs_diff:.0f} 元({pct*100:.1f}%), 规则[{rule}]"
        )
        logger.warning(f"[anomaly] {product_name} @ {platform}: {reason}")
        return True, reason

    return False, ""


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
