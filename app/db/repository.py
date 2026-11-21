"""价格数据存取层：产品与价格历史的读写

不依赖任何具体数据源（电商爬虫 / B站视频 / 手工录入都能用）
"""
import json
import re
from datetime import date, timedelta

from sqlalchemy import and_

from app.db.database import SessionLocal
from app.db.models import Product, PriceHistory, VideoDigest, TranscriptSegment
from app.core.logger import logger

# 价格浮动告警阈值(元)：与上次价格差超过此值则告警
PRICE_ALERT_THRESHOLD = 1000


def _infer_category(name: str) -> str:
    """
    按型号名推断品类

    视频抽出来的只有型号名，不带品类。DDR 开头是内存，PCIe/NVMe 是硬盘，
    Intel i/U 系列和 AMD R 系列是 CPU，其余（RTX/RX/ARC）按显卡处理。
    """
    upper = name.upper()
    if "DDR" in upper or "内存" in name:
        return "memory"
    if "PCIE" in upper or "NVME" in upper or "SSD" in upper or "硬盘" in name:
        return "ssd"
    # 必须带连字符，否则显卡 RX 9070 会被当成 R7- 系列 CPU
    if re.match(r"^(I[3579]|R[3579]|U[3579])-", upper):
        return "cpu"
    return "gpu"


def save_price(product_name: str, platform: str, price: float,
               category: str = "", brand: str = "", specs: str = "",
               condition: str = "new", change: float = None,
               change_pct: float = None, record_date: date = None) -> dict:
    """
    保存一条价格记录

    - 按 name + platform + condition 查 Product：存在则更新 current_price，不存在则新建
    - PriceHistory：同一天同产品同平台只保留一条（重复抓取则覆盖）
    - 与上一条价格对比，浮动超过阈值时返回告警

    :param category: 不传则按型号名推断（gpu/memory/ssd）
    :param condition: new 全新 / used 二手。二手价和全新价是两条独立记录
    :param change: 视频表格里的当日涨跌幅（元），原样入库，不用历史价推算
    :param change_pct: 涨跌百分比，表格没有就不传
    :param record_date: 数据所属日期，默认今天。补跑历史视频时传发布日期，
                        否则历史价格会被写到今天名下
    :return: {"alert": 告警信息或 None, "change": 涨跌金额, "change_pct": 涨跌百分比}
    """
    db = SessionLocal()
    alert_msg = None
    record_date = record_date or date.today()
    is_latest = True
    try:
        product = db.query(Product).filter(
            and_(
                Product.name == product_name,
                Product.platform == platform,
                Product.condition == condition,
            )
        ).first()

        if product is None:
            product = Product(
                name=product_name,
                category=category or _infer_category(product_name),
                brand=brand,
                specs=specs,
                current_price=price,
                platform=platform,
                condition=condition,
            )
            db.add(product)
            db.flush()
            logger.info(f"[repo] 新建产品: {product_name} @ {platform}({condition})")
        else:
            # 补跑历史视频时，current_price 不能被旧价格覆盖
            latest_date = (
                db.query(PriceHistory.date)
                .filter(and_(
                    PriceHistory.product_id == product.id,
                    PriceHistory.platform == platform,
                ))
                .order_by(PriceHistory.date.desc())
                .first()
            )
            is_latest = latest_date is None or record_date >= latest_date[0]
            if is_latest:
                product.current_price = price
            if brand and not product.brand:
                product.brand = brand
            if specs and not product.specs:
                product.specs = specs

        # 前一天的价格只用于异常告警；涨跌幅直接用视频表格里的值
        prev_record = (
            db.query(PriceHistory)
            .filter(and_(
                PriceHistory.product_id == product.id,
                PriceHistory.platform == platform,
                PriceHistory.date < record_date,
            ))
            .order_by(PriceHistory.date.desc())
            .first()
        )

        if prev_record and prev_record.price:
            diff = price - prev_record.price
            if abs(diff) > PRICE_ALERT_THRESHOLD:
                direction = "上涨" if diff > 0 else "下跌"
                alert_msg = (
                    f"价格异常波动: {product_name} @ {platform} "
                    f"{direction} {abs(diff):.0f} 元 "
                    f"(上次: {prev_record.price:.0f} → 本次: {price:.0f})"
                )
                logger.warning(f"[repo] {alert_msg}")

        # 该日期已有记录则更新，否则插入
        day_record = (
            db.query(PriceHistory)
            .filter(and_(
                PriceHistory.product_id == product.id,
                PriceHistory.platform == platform,
                PriceHistory.date == record_date,
            ))
            .first()
        )

        if day_record:
            day_record.price = price
            day_record.change = change
            day_record.change_pct = change_pct
            logger.info(f"[repo] 更新价格: {product_name} @ {platform} {record_date} = {price}")
        else:
            db.add(PriceHistory(
                product_id=product.id,
                price=price,
                change=change,
                change_pct=change_pct,
                platform=platform,
                date=record_date,
            ))
            logger.info(f"[repo] 新增价格记录: {product_name} @ {platform} {record_date} = {price}")

        # 产品表同步最新涨跌，列表页直接读，不用再查历史
        if is_latest:
            product.change = change
            product.change_pct = change_pct

        db.commit()
        return {"alert": alert_msg, "change": change, "change_pct": change_pct}

    except Exception as e:
        db.rollback()
        logger.error(f"[repo] 入库失败: {e}")
        return {"alert": f"入库失败: {e}", "change": None, "change_pct": None}
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


def save_digest(bvid: str, video_date: date, title: str,
                market_summary: str, news: list) -> None:
    """
    保存一期视频的文字归档（价格归纳 + 硬件新闻）

    按 bvid 覆盖而不是追加：同一期视频反复采集时内容相同，
    追加会让新闻重复出现。
    """
    db = SessionLocal()
    try:
        row = db.query(VideoDigest).filter(VideoDigest.bvid == bvid).first()
        if row is None:
            row = VideoDigest(bvid=bvid)
            db.add(row)
        row.video_date = video_date
        row.title = title
        row.market_summary = market_summary or ""
        row.news = json.dumps(news or [], ensure_ascii=False)
        db.commit()
        logger.info(f"[repo] 保存文字归档: {bvid} ({video_date})")
    except Exception as e:
        db.rollback()
        logger.error(f"[repo] 保存文字归档失败: {e}")
    finally:
        db.close()


def save_transcript(bvid: str, video_date: date, segments: list) -> int:
    """
    保存一期视频的 ASR 转写分段

    按 bvid 整体覆盖：重新转写时分段边界会变化，逐条 upsert 会留下旧片段。
    转写为空时直接返回，不删除已有数据，避免一次 ASR 失败把上次的结果清掉。

    :param segments: [{"start": 秒, "end": 秒, "text": 文本}]
    :return: 实际写入的分段数
    """
    rows = []
    for seg in segments or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        rows.append({
            "start": float(seg.get("start") or 0),
            "end": float(seg.get("end") or 0),
            "text": text,
        })

    if not rows:
        logger.warning(f"[repo] {bvid} 转写为空，跳过保存")
        return 0

    db = SessionLocal()
    try:
        db.query(TranscriptSegment).filter(TranscriptSegment.bvid == bvid).delete()
        for row in rows:
            db.add(TranscriptSegment(
                bvid=bvid,
                video_date=video_date,
                start_sec=row["start"],
                end_sec=row["end"],
                text=row["text"],
            ))
        db.commit()
        logger.info(f"[repo] 保存转写分段: {bvid} ({video_date}) {len(rows)} 段")
        return len(rows)
    except Exception as e:
        db.rollback()
        logger.error(f"[repo] 保存转写分段失败: {e}")
        return 0
    finally:
        db.close()


def _digest_to_dict(row) -> dict:
    try:
        news = json.loads(row.news or "[]")
    except json.JSONDecodeError:
        news = []
    return {
        "date": row.video_date,
        "title": row.title,
        "market_summary": row.market_summary,
        "news": news,
    }


def get_digest(day: date = None) -> dict | None:
    """取某天的价格归纳；不传日期取最近一期"""
    db = SessionLocal()
    try:
        query = db.query(VideoDigest)
        if day:
            query = query.filter(VideoDigest.video_date == day)
        row = query.order_by(VideoDigest.video_date.desc()).first()
        return _digest_to_dict(row) if row else None
    finally:
        db.close()


def search_news(keyword: str = "", days: int = 7) -> list:
    """
    检索最近几期的硬件新闻

    关键词为空时返回最近几期的全部新闻，用于"最近有什么硬件新闻"这类问题。
    """
    since = date.today() - timedelta(days=days)
    db = SessionLocal()
    try:
        rows = (
            db.query(VideoDigest)
            .filter(VideoDigest.video_date >= since)
            .order_by(VideoDigest.video_date.desc())
            .all()
        )
        results = []
        seen = set()
        for row in rows:
            for item in _digest_to_dict(row)["news"]:
                title = item.get("title", "")
                text = f"{title}{item.get('detail', '')}"
                if keyword and keyword not in text:
                    continue
                # 同一件事会被连续几期日报反复提到，按标题去重只留最新一次
                if title in seen:
                    continue
                seen.add(title)
                results.append({
                    "date": row.video_date.isoformat(),
                    "title": title,
                    "detail": item.get("detail", ""),
                    "video": row.title,
                })
        return results
    finally:
        db.close()


def get_platform_prices(product_name: str, condition: str = "new") -> dict:
    """
    查询某产品在各平台的最新价格

    :param condition: new 全新 / used 二手，默认真新价，避免闲鱼二手价混进比价
    :return: {"name", "prices": {"jd": ..}, "lowest_platform", "lowest_price", ...}
    """
    db = SessionLocal()
    try:
        std_name = _resolve_product_name(db, product_name)
        if not std_name:
            return {}

        rows = db.query(Product).filter(
            and_(Product.name == std_name, Product.condition == condition)
        ).all()
        prices = {}
        changes = {}
        brands = set()
        for p in rows:
            if p.current_price:
                prices[p.platform] = p.current_price
                changes[p.platform] = {"change": p.change, "change_pct": p.change_pct}
            if p.brand:
                brands.add(p.brand)

        if not prices:
            return {}

        cheapest_platform = min(prices, key=prices.get)
        return {
            "name": std_name,
            "category": rows[0].category,
            "condition": condition,
            "brands": list(brands),
            "prices": prices,
            "changes": changes,
            "lowest_platform": cheapest_platform,
            "lowest_price": prices[cheapest_platform],
            "lowest_change": changes[cheapest_platform]["change"],
            "lowest_change_pct": changes[cheapest_platform]["change_pct"],
        }
    finally:
        db.close()


def get_price_history(product_name: str, platform: str = None, days: int = 30,
                      condition: str = "new") -> list:
    """
    查询产品价格历史（按日期升序）

    :param platform: 指定平台，不传则聚合所有平台
    :param condition: new 全新 / used 二手
    :return: [{"date": datetime, "price": float, "platform": str}, ...]
    """
    db = SessionLocal()
    try:
        std_name = _resolve_product_name(db, product_name)
        if not std_name:
            return []

        product_ids = [
            r[0] for r in db.query(Product.id).filter(
                and_(Product.name == std_name, Product.condition == condition)
            ).all()
        ]
        if not product_ids:
            return []

        query = db.query(PriceHistory).filter(PriceHistory.product_id.in_(product_ids))
        if platform:
            query = query.filter(PriceHistory.platform == platform)
        rows = query.order_by(PriceHistory.date.desc()).limit(days).all()
        rows = list(reversed(rows))

        return [
            {"date": r.date, "price": r.price, "platform": r.platform,
             "change": r.change, "change_pct": r.change_pct}
            for r in rows
        ]
    finally:
        db.close()
