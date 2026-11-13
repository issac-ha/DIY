"""数据库模型：硬件产品与价格历史"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from app.db.database import Base


class Product(Base):
    """硬件产品表：显卡 / 内存"""
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, index=True)          # 产品名，如 RTX 4060
    category = Column(String(20), nullable=False, index=True)       # gpu / memory
    brand = Column(String(50))                                      # 品牌
    specs = Column(String(500))                                     # 核心规格，文字描述
    current_price = Column(Float)                                   # 当前价格

    price_history = relationship("PriceHistory", back_populates="product", cascade="all, delete-orphan")


class PriceHistory(Base):
    """价格历史表：记录每天的价格，用于趋势分析"""
    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    price = Column(Float, nullable=False)
    date = Column(DateTime, nullable=False, default=datetime.now)

    product = relationship("Product", back_populates="price_history")
