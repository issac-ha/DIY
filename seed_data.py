"""
初始化数据库：建表 + 写入模拟数据

运行：python seed_data.py
"""
import random
from datetime import datetime, timedelta

from app.db.database import engine, SessionLocal, Base
from app.db.models import Product, PriceHistory


# 模拟硬件数据：名称、分类、品牌、规格、当前价格
# 后面会替换成爬虫采集的真实数据
PRODUCTS = [
    # 显卡
    {"name": "RTX 4060", "category": "gpu", "brand": "NVIDIA",
     "specs": "8GB GDDR6, 3072 CUDA 核心", "current_price": 2399},
    {"name": "RTX 4070 Super", "category": "gpu", "brand": "NVIDIA",
     "specs": "12GB GDDR6X, 7168 CUDA 核心", "current_price": 4799},
    {"name": "RTX 5070", "category": "gpu", "brand": "NVIDIA",
     "specs": "12GB GDDR7, 6144 CUDA 核心", "current_price": 4299},
    {"name": "RTX 5080", "category": "gpu", "brand": "NVIDIA",
     "specs": "16GB GDDR7, 10752 CUDA 核心", "current_price": 8299},
    {"name": "RX 7600", "category": "gpu", "brand": "AMD",
     "specs": "8GB GDDR6, 2048 流处理器", "current_price": 2099},
    {"name": "RX 7700 XT", "category": "gpu", "brand": "AMD",
     "specs": "12GB GDDR6, 3456 流处理器", "current_price": 3299},

    # 内存
    {"name": "DDR5 6000 16GB", "category": "memory", "brand": "金士顿",
     "specs": "16GB(8Gx2), DDR5 6000MHz", "current_price": 350},
    {"name": "DDR5 6400 32GB", "category": "memory", "brand": "芝奇",
     "specs": "32GB(16Gx2), DDR5 6400MHz", "current_price": 720},
    {"name": "DDR4 3200 16GB", "category": "memory", "brand": "威刚",
     "specs": "16GB(8Gx2), DDR4 3200MHz", "current_price": 200},
    {"name": "DDR5 7200 32GB", "category": "memory", "brand": "海盗船",
     "specs": "32GB(16Gx2), DDR5 7200MHz", "current_price": 1200},
]


def seed():
    # 1. 建表：如果表不存在就创建
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()

    # 2. 如果已有数据就清空（方便重复运行）
    db.query(PriceHistory).delete()
    db.query(Product).delete()
    db.commit()

    # 3. 写入产品和 30 天历史价格
    today = datetime.now()
    for data in PRODUCTS:
        product = Product(**data)
        db.add(product)
        db.flush()  # 拿到 product.id

        # 生成过去 30 天的价格：在当前价格附近随机波动
        base_price = data["current_price"]
        for i in range(30, 0, -1):
            date = today - timedelta(days=i)
            # 价格波动幅度 +/-5%
            fluctuation = random.uniform(-0.05, 0.05)
            price = round(base_price * (1 + fluctuation), 2)
            db.add(PriceHistory(product_id=product.id, price=price, date=date))

    db.commit()
    print(f"初始化完成：{len(PRODUCTS)} 个产品，每个 30 天历史价格")
    db.close()


if __name__ == "__main__":
    seed()
