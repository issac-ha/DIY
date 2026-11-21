"""数据库模型：硬件产品与价格历史"""
from datetime import date as date_type, datetime
from sqlalchemy import Column, Integer, String, Float, Date, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from app.db.database import Base


class Product(Base):
    """硬件产品表：显卡 / 内存 / 硬盘"""
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, index=True)          # 产品名，如 RTX 4060 / DDR5 32GB*2
    category = Column(String(20), nullable=False, index=True)       # gpu / cpu / memory / ssd
    brand = Column(String(50))                                      # 品牌
    specs = Column(String(500))                                     # 核心规格，文字描述
    current_price = Column(Float)                                   # 当前价格
    # 最新价相对前一天同产品同平台的变化：正为涨、负为跌。
    # 列表页要展示涨跌幅，直接从产品表读，避免每次遍历整张历史表。
    change = Column(Float)
    change_pct = Column(Float)
    platform = Column(String(20), nullable=False, default='taobao')    # 平台来源：taobao/pdd/jd/douyin/xianyu
    # 成色：new 全新 / used 二手
    # 闲鱼卖的是二手成交价，同一型号的二手价必然低于全新价。
    # 不区分成色的话，比价时闲鱼永远是最低价，会把"全新卡该买哪个平台"算错。
    condition = Column(String(10), nullable=False, default="new", index=True)
    # 内存/硬盘日报表才有：今日最低店铺、今日最低型号（显卡表留空）
    # 存成产品字段而不是价格字段，因为它们是当天全平台的结论，不随单个平台变化
    lowest_shop = Column(String(100))
    lowest_model = Column(String(100))

    # 联合唯一：同一成色、同一平台的同一产品只存一条
    __table_args__ = (
        UniqueConstraint('name', 'platform', 'condition', name='uq_name_platform_condition'),
    )

    price_history = relationship("PriceHistory", back_populates="product", cascade="all, delete-orphan")


class PriceHistory(Base):
    """价格历史表：记录每天的价格，用于趋势分析"""
    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    price = Column(Float, nullable=False)
    # 当日相对前一天的涨跌。存进历史记录，趋势图可以直接标出每天的涨跌，
    # 不用每次查询再倒推一遍。
    change = Column(Float)              # 涨跌金额（元）
    change_pct = Column(Float)          # 涨跌百分比
    date = Column(Date, nullable=False, default=date_type.today)
    platform = Column(String(20), nullable=False)                   # 平台来源

    product = relationship("Product", back_populates="price_history")


class VideoDigest(Base):
    """
    视频文字内容存档：一天的价格归纳 + 硬件新闻

    这两块来自视频旁白（ASR），回答"今天行情怎么样""最近有什么硬件新闻"这类
    没有具体型号的问题时，价格表用不上，只能靠这里。按 bvid 唯一，
    同一期视频重复采集时覆盖，不会重复累积。
    """
    __tablename__ = "video_digests"

    id = Column(Integer, primary_key=True, index=True)
    bvid = Column(String(20), nullable=False, unique=True, index=True)
    video_date = Column(Date, nullable=False, index=True)
    title = Column(String(300))
    # 当日价格归纳：大盘指数、涨跌板块、点名的型号和价格
    market_summary = Column(Text)
    # 硬件新闻：JSON 数组，每项 {"title": 标题, "detail": 要点}
    news = Column(Text)
    created_at = Column(DateTime, default=datetime.now)


class TranscriptSegment(Base):
    """
    ASR 转写分段：保留视频原话和时间戳

    VideoDigest 只存模型压缩后的摘要（一百多字），判断依据和上下文都丢了。
    原始转写单独存表，供语义检索和"视频里具体怎么说的"这类问题引用；
    保留 start/end 后，回答可以标到具体时间点。
    按 bvid 整体覆盖，避免重新转写时分段边界变化导致旧片段残留。
    """
    __tablename__ = "transcript_segments"

    id = Column(Integer, primary_key=True, index=True)
    bvid = Column(String(20), nullable=False, index=True)
    video_date = Column(Date, nullable=False, index=True)
    start_sec = Column(Float, nullable=False, default=0)
    end_sec = Column(Float, nullable=False, default=0)
    text = Column(Text, nullable=False)
