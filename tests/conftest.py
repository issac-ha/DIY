"""
pytest 全局配置

必须在导入 app 包之前改写 DATABASE_URL：
app/db/database.py 在 import 时就会 create_engine，一旦绑定到 .env 里的
MySQL，测试就会直接写生产库。所以这里先设置环境变量，再导入应用模块。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 测试库用独立 SQLite 文件，跑完删除，和容器里的 MySQL 完全隔离
TEST_DB = ROOT / "_test_diy.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"

# 这些 Key 只用于满足构造校验，测试不会发真实请求
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("VISION_API_KEY", "test-key")
os.environ.setdefault("BILIBILI_COOKIE", "test-cookie")
os.environ.setdefault("API_KEY", "")

import pytest

from app.db import models  # noqa: F401  导入模型才能把表注册到 Base.metadata
from app.db.database import Base, SessionLocal, engine


@pytest.fixture(scope="session", autouse=True)
def prepare_database():
    """整个测试会话开始时建表，结束时删表并清理文件"""
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)
    engine.dispose()
    if TEST_DB.exists():
        TEST_DB.unlink()


@pytest.fixture(autouse=True)
def clean_tables():
    """每个测试前清空所有表，避免用例之间互相污染"""
    session = SessionLocal()
    try:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
    finally:
        session.close()
    yield


@pytest.fixture
def db():
    """给测试直接查询用的 session，用完关闭"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
