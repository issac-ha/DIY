"""数据库连接与 Session

DATABASE_URL 决定用 SQLite 还是 MySQL：
- 本地开发默认 SQLite，零配置
- 部署时改成 mysql+pymysql://user:password@host:3306/dbname?charset=utf8mb4
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from app.config import DATABASE_URL


def _engine_options(url: str) -> dict:
    """按数据库类型给出连接参数，避免 SQLite 和 MySQL 共用一套配置"""
    if url.startswith("sqlite"):
        # SQLite 默认只允许创建连接的那个线程使用它，而 FastAPI 和采集线程池
        # 都会跨线程访问，所以必须关掉这个限制
        return {"connect_args": {"check_same_thread": False}}

    # MySQL 服务端会主动断开空闲连接，取连接前先探活；连接寿命控制在一小时内。
    # charset 用 utf8mb4，否则新闻里的表情符号和部分中文会被截断
    return {
        "pool_pre_ping": True,
        "pool_recycle": 3600,
        "connect_args": {"charset": "utf8mb4"},
    }


engine = create_engine(DATABASE_URL, **_engine_options(DATABASE_URL))
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI 依赖：每次请求新建一个 db session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
