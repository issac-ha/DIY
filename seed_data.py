"""
初始化数据库：用基准价格表（app/data/benchmark_prices.json）建表并写入数据

每个型号的每个平台各存一条 Product 记录，并生成过去 30 天的价格历史。
运行：python seed_data.py
"""
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from app.db.database import engine, SessionLocal, Base
from app.db.models import Product, PriceHistory

BENCHMARK_FILE = Path(__file__).resolve().parent / "app" / "data" / "benchmark_prices.json"

# 平台中文名，仅用于日志
PLATFORM_NAMES = {"jd": "京东", "taobao": "淘宝", "pdd": "拼多多", "douyin": "抖音"}


def load_products() -> list:
    """从基准价格表读取产品，展开成 (型号, 平台, 价格) 列表"""
    data = json.loads(BENCHMARK_FILE.read_text(encoding="utf-8"))
    items = []
    for name, info in data["products"].items():
        for platform, price in info.get("prices", {}).items():
            if not price:  # 0 表示无货
                continue
            items.append({
                "name": name,
                "platform": platform,
                "price": float(price),
                "brand": info.get("brand", ""),
                "specs": f"TSE 跑分 {info.get('tse_score', '')}",
                "category": "gpu",
            })
    return items


def seed():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    # 清空旧数据，方便重复运行
    db.query(PriceHistory).delete()
    db.query(Product).delete()
    db.commit()

    products = load_products()
    today = datetime.now()

    for data in products:
        product = Product(
            name=data["name"],
            category=data["category"],
            brand=data["brand"],
            specs=data["specs"],
            current_price=data["price"],
            platform=data["platform"],
        )
        db.add(product)
        db.flush()

        # 过去 30 天历史：在当前价附近 ±3% 波动
        for i in range(30, 0, -1):
            date = today - timedelta(days=i)
            price = round(data["price"] * (1 + random.uniform(-0.03, 0.03)), 2)
            db.add(PriceHistory(
                product_id=product.id,
                price=price,
                date=date,
                platform=data["platform"],
            ))

    db.commit()

    names = len({p["name"] for p in products})
    platforms = len({p["platform"] for p in products})
    print(f"初始化完成：{names} 个型号，{len(products)} 条平台记录（{platforms} 个平台），每条 30 天历史")
    db.close()


if __name__ == "__main__":
    seed()
