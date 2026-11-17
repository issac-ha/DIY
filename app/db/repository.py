"""价格数据存取层：产品与价格历史的读写

不依赖任何具体数据源（电商爬虫 / B站视频 / 手工录入都能用）
"""
from datetime import datetime

from sqlalchemy import and_

from app.db.database import SessionLocal
from app.db.models import Product, PriceHistory
from app.core.logger import logger

# 价格浮动告警阈值(元)：与上次价格差超过此值则告警
PRICE_ALERT_THRESHOLD = 1000


def save_price(product_name: str, platform: str, price: float,
               category: str = "gpu", brand: str = "", specs: str = "") -> str:
    """
    保存一条价格记录

    - 按 name + platform 查 Product：存在则更新 current_price，不存在则新建
    - PriceHistory：同一天同产品同平台只保留一条（重复抓取则覆盖）
    - 与上一条价格对比，浮动超过阈值时返回告警

    :return: 告警信息（无告警返回 None）
    """
    db = SessionLocal()
    alert_msg = None
    try:
        product = db.query(Product).filter(
            and_(Product.name == product_name, Product.platform == platform)
        ).first()

        if product is None:
            product = Product(
                name=product_name,
                category=category,
                brand=brand,
                specs=specs,
                current_price=price,
                platform=platform,
            )
            db.add(product)
            db.flush()
            logger.info(f"[repo] 新建产品: {product_name} @ {platform}")
        else:
            product.current_price = price
            if brand and not product.brand:
                product.brand = brand
            if specs and not product.specs:
                product.specs = specs

        # 上一条历史价格（用于浮动检测）
        last_record = (
            db.query(PriceHistory)
            .filter(and_(
                PriceHistory.product_id == product.id,
                PriceHistory.platform == platform,
            ))
            .order_by(PriceHistory.date.desc())
            .first()
        )

        if last_record:
            diff = price - last_record.price
            if abs(diff) > PRICE_ALERT_THRESHOLD:
                direction = "上涨" if diff > 0 else "下跌"
                alert_msg = (
                    f"价格异常波动: {product_name} @ {platform} "
                    f"{direction} {abs(diff):.0f} 元 "
                    f"(上次: {last_record.price:.0f} → 本次: {price:.0f})"
                )
                logger.warning(f"[repo] {alert_msg}")

        # 今天已有记录则更新，否则插入
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_record = (
            db.query(PriceHistory)
            .filter(and_(
                PriceHistory.product_id == product.id,
                PriceHistory.platform == platform,
                PriceHistory.date >= today_start,
            ))
            .first()
        )

        if today_record:
            today_record.price = price
            logger.info(f"[repo] 更新今日价格: {product_name} @ {platform} = {price}")
        else:
            db.add(PriceHistory(
                product_id=product.id,
                price=price,
                platform=platform,
                date=datetime.now(),
            ))
            logger.info(f"[repo] 新增价格记录: {product_name} @ {platform} = {price}")

        db.commit()
        return alert_msg

    except Exception as e:
        db.rollback()
        logger.error(f"[repo] 入库失败: {e}")
        return f"入库失败: {e}"
    finally:
        db.close()


def _resolve_product_name(db, product_name: str):
    """
    把用户输入解析成数据库里的标准型号名

    规则：先精确匹配；没有再模糊匹配，取最短的匹配名
    （避免查 "RTX 5070" 时混进 "RTX 5070 Ti" 的数据）
    """
    exact = db.query(Product.name).filter(Product.name == product_name).first()
    if exact:
        return exact[0]

    rows = db.query(Product.name).filter(Product.name.contains(product_name)).distinct().all()
    if not rows:
        return None
    return min((r[0] for r in rows), key=len)


def get_platform_prices(product_name: str) -> dict:
    """
    查询某产品在各平台的最新价格

    :return: {"name", "prices": {"jd": ..}, "lowest_platform", "lowest_price", ...}
    """
    db = SessionLocal()
    try:
        std_name = _resolve_product_name(db, product_name)
        if not std_name:
            return {}

        rows = db.query(Product).filter(Product.name == std_name).all()
        prices = {}
        brands = set()
        for p in rows:
            if p.current_price:
                prices[p.platform] = p.current_price
            if p.brand:
                brands.add(p.brand)

        if not prices:
            return {}

        cheapest_platform = min(prices, key=prices.get)
        return {
            "name": std_name,
            "category": rows[0].category,
            "brands": list(brands),
            "prices": prices,
            "lowest_platform": cheapest_platform,
            "lowest_price": prices[cheapest_platform],
        }
    finally:
        db.close()


def get_price_history(product_name: str, platform: str = None, days: int = 30) -> list:
    """
    查询产品价格历史（按日期升序）

    :param platform: 指定平台，不传则聚合所有平台
    :return: [{"date": datetime, "price": float, "platform": str}, ...]
    """
    db = SessionLocal()
    try:
        std_name = _resolve_product_name(db, product_name)
        if not std_name:
            return []

        product_ids = [
            r[0] for r in db.query(Product.id).filter(Product.name == std_name).all()
        ]
        if not product_ids:
            return []

        query = db.query(PriceHistory).filter(PriceHistory.product_id.in_(product_ids))
        if platform:
            query = query.filter(PriceHistory.platform == platform)
        rows = query.order_by(PriceHistory.date.desc()).limit(days).all()
        rows = list(reversed(rows))

        return [
            {"date": r.date, "price": r.price, "platform": r.platform}
            for r in rows
        ]
    finally:
        db.close()
