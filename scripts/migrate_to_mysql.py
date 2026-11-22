"""SQLite 到 MySQL 的一次性数据迁移

用法：
    python -m scripts.migrate_to_mysql --target "mysql+pymysql://root@127.0.0.1:3306/diy_agent?charset=utf8mb4"

两个关键设计：

1. 保留源库主键 id
price_history 通过 product_id 关联 products。如果交给 MySQL 重新分配自增 id，
价格记录就会挂到别的型号上，比价和趋势全部错位。所以插入时显式带上 id。

2. 按依赖顺序复制
先 products 再 price_history，否则外键约束会直接拒绝插入。

脚本支持重复执行：目标库已存在的 id 会跳过，中断后重跑不会产生重复数据。
"""
import argparse

from sqlalchemy import create_engine, insert, select, func

from app.db.database import Base
from app.db.models import Product, PriceHistory, TranscriptSegment, VideoDigest

# 复制顺序就是依赖顺序，被引用的表必须在前
TABLES = [Product, PriceHistory, VideoDigest, TranscriptSegment]

# 单批插入条数。行数上万时一次性 executemany 会让驱动拼出超长 SQL，
# 分批可以控制语句大小，也方便观察进度
BATCH_SIZE = 500


def migrate(source_url: str, target_url: str, truncate: bool = False) -> None:
    src_engine = create_engine(source_url)
    dst_engine = create_engine(target_url, pool_pre_ping=True)

    # 目标库还没建表时按模型补建，重复调用是安全的
    Base.metadata.create_all(bind=dst_engine)

    for model in TABLES:
        name = model.__tablename__
        with src_engine.connect() as src, dst_engine.begin() as dst:
            if truncate:
                dst.execute(model.__table__.delete())

            rows = src.execute(select(model)).mappings().all()
            existing = {row_id for (row_id,) in dst.execute(select(model.id))}
            pending = [dict(row) for row in rows if row["id"] not in existing]

            for start in range(0, len(pending), BATCH_SIZE):
                dst.execute(insert(model), pending[start:start + BATCH_SIZE])

            total = dst.execute(select(func.count()).select_from(model)).scalar()

        print(f"{name}: 源库 {len(rows)} 行，本次写入 {len(pending)} 行，目标库现有 {total} 行")

    src_engine.dispose()
    dst_engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLite 到 MySQL 数据迁移")
    parser.add_argument("--source", default="sqlite:///./diy_agent.db", help="源库连接串")
    parser.add_argument("--target", required=True, help="目标库连接串")
    parser.add_argument("--truncate", action="store_true", help="迁移前先清空目标表")
    args = parser.parse_args()

    if not args.target.startswith("mysql"):
        raise SystemExit("目标库必须是 MySQL 连接串")
    if args.source.startswith("mysql"):
        raise SystemExit("暂不支持 MySQL 到 MySQL 迁移，避免误清数据")

    migrate(args.source, args.target, args.truncate)


if __name__ == "__main__":
    main()
