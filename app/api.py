"""FastAPI 后端接口"""
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func

from app.agent.hardware_agent import agent
from app.db import repository
from app.db.database import SessionLocal
from app.db.models import Product, PriceHistory

app = FastAPI(title="DIY Agent - 硬件价格分析")

# 允许跨域，前端（含本地文件调试）可直接调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

PLATFORM_NAMES = {"jd": "京东", "taobao": "淘宝", "pdd": "拼多多",
                  "douyin": "抖音", "xianyu": "闲鱼"}
REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports"


class ChatMessage(BaseModel):
    """一轮对话消息，role 是 user 或 assistant"""
    role: str
    content: str


class ChatRequest(BaseModel):
    """
    对话请求

    messages 是完整上下文：多轮追问要靠它，否则模型记不住用户刚说过的预算和用途。
    message 保留给旧调用方，只传一句话时仍可用。
    """
    message: str = ""
    messages: list[ChatMessage] = Field(default_factory=list)


class ChatResponse(BaseModel):
    """返回给前端的数据结构"""
    reply: str


# 只带最近若干轮，避免上下文无限增长推高 token 成本
MAX_CHAT_MESSAGES = 20


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    """对话接口：接收多轮消息，返回 Agent 回答"""
    if request.messages:
        history = [
            {"role": item.role, "content": item.content}
            for item in request.messages
            if item.role in ("user", "assistant") and item.content.strip()
        ][-MAX_CHAT_MESSAGES:]
    elif request.message.strip():
        history = [{"role": "user", "content": request.message.strip()}]
    else:
        raise HTTPException(status_code=400, detail="消息不能为空")

    if not history:
        raise HTTPException(status_code=400, detail="消息不能为空")

    result = agent.invoke({"messages": history})
    reply = result["messages"][-1].content
    return ChatResponse(reply=reply)


@app.get("/api/trend")
def get_trend(name: str, days: int = 7, condition: str = "new"):
    """
    返回产品最近 N 天的价格趋势，分平台给出序列，供前端画多线对比图

    condition: new 全新（默认）/ used 二手。不传全新，否则闲鱼二手价会串进比价曲线
    """
    db = SessionLocal()
    try:
        # 先按完整型号名匹配。前端点表格行传的是标准名，
        # 用 contains 会把 "RTX 5070" 和 "RTX 5070 Ti" 的曲线混在一起。
        products = db.query(Product).filter(
            Product.name == name,
            Product.condition == condition,
        ).all()
        if not products:
            # 兼容聊天里输入的部分型号，比如只输入 "5070"
            products = db.query(Product).filter(
                Product.name.contains(name),
                Product.condition == condition,
            ).all()
        if not products:
            return {"error": f"没有找到 {name}"}

        ids = [p.id for p in products]
        histories = (
            db.query(PriceHistory)
            .filter(PriceHistory.product_id.in_(ids))
            .order_by(PriceHistory.date.asc())
            .all()
        )

        # platform -> {日期: 价格}，以及每位价格的当日涨跌
        by_platform = {}
        change_by_platform = {}
        for h in histories:
            day = h.date.strftime("%m-%d")
            by_platform.setdefault(h.platform, {})[day] = h.price
            change_by_platform.setdefault(h.platform, {})[day] = {
                "change": h.change,
                "change_pct": h.change_pct,
            }

        all_dates = sorted({d for m in by_platform.values() for d in m})[-days:]

        series = []
        for platform, price_map in by_platform.items():
            series.append({
                "platform": platform,
                "name": PLATFORM_NAMES.get(platform, platform),
                "prices": [price_map.get(d) for d in all_dates],
                "changes": [
                    change_by_platform.get(platform, {}).get(d) for d in all_dates
                ],
            })

        # 每天的全网最低价
        lowest = []
        for d in all_dates:
            day_prices = [m[d] for m in by_platform.values() if d in m]
            lowest.append(min(day_prices) if day_prices else None)

        return {
            "name": products[0].name,
            "dates": all_dates,
            "prices": lowest,
            "series": series,
        }
    finally:
        db.close()


@app.get("/api/products")
def list_products(category: str = "", condition: str = "new"):
    """产品列表（按型号聚合，给出各平台价格与最低价）

    condition: new 全新（默认）/ used 二手
    """
    db = SessionLocal()
    try:
        query = db.query(Product).filter(Product.condition == condition)
        if category:
            query = query.filter(Product.category == category)
        rows = query.all()
    finally:
        db.close()

    grouped = {}
    changes = {}
    for p in rows:
        if p.current_price:
            grouped.setdefault(p.name, {})[p.platform] = p.current_price
            changes.setdefault(p.name, {})[p.platform] = {
                "change": p.change,
                "change_pct": p.change_pct,
            }

    result = []
    for name, prices in sorted(grouped.items()):
        platform = min(prices, key=prices.get)
        low_change = changes.get(name, {}).get(platform, {})
        result.append({
            "name": name,
            "prices": prices,
            "changes": changes.get(name, {}),
            "lowest_platform": platform,
            "lowest_price": prices[platform],
            "lowest_change": low_change.get("change"),
            "lowest_change_pct": low_change.get("change_pct"),
        })
    return {"products": result}


@app.get("/api/dates")
def list_dates(limit: int = 30):
    """前端日期下拉框的数据：价格历史里有哪些天，最近的排在前面"""
    db = SessionLocal()
    try:
        rows = (
            db.query(PriceHistory.date, func.count(PriceHistory.id))
            .group_by(PriceHistory.date)
            .order_by(PriceHistory.date.desc())
            .limit(limit)
            .all()
        )
    finally:
        db.close()
    return {"dates": [{"date": d.isoformat(), "count": c} for d, c in rows]}


@app.get("/api/prices")
def list_prices(day: str = "", category: str = "", condition: str = "new"):
    """
    某一天的比价表

    和 /api/products 的区别：这里读的是当天 PriceHistory，而不是产品表的
    current_price。切换日期能看到不同天的价格，点表格行再画走势图。

    :param day: YYYY-MM-DD，不传取数据库里最新一天
    :param category: gpu / cpu / memory / ssd，不传返回全部品类
    :param condition: new 全新 / used 二手
    """
    target = None
    if day:
        try:
            target = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            return {"error": f"日期格式不对：{day}，应为 YYYY-MM-DD"}

    db = SessionLocal()
    try:
        if target is None:
            target = db.query(func.max(PriceHistory.date)).scalar()
        if target is None:
            return {"date": None, "platforms": [], "products": []}

        query = (
            db.query(PriceHistory, Product)
            .join(Product, PriceHistory.product_id == Product.id)
            .filter(PriceHistory.date == target, Product.condition == condition)
        )
        if category:
            query = query.filter(Product.category == category)
        rows = query.all()

        grouped = {}
        meta = {}
        platforms = set()
        for history, product in rows:
            if not history.price:
                continue
            platforms.add(history.platform)
            grouped.setdefault(product.name, {})[history.platform] = history.price
            # 涨跌是"当天相对前一天"的，跟着日期一起取，不能用产品表的最新值
            meta.setdefault(product.name, {})
            meta[product.name][history.platform] = {
                "change": history.change,
                "change_pct": history.change_pct,
            }
            # 最低店铺/型号是内存、硬盘表才有的当日结论
            meta[product.name]["lowest_shop"] = product.lowest_shop
            meta[product.name]["lowest_model"] = product.lowest_model

        results = []
        for name, prices in grouped.items():
            lowest_platform = min(prices, key=prices.get)
            low_change = meta[name].get(lowest_platform) or {}
            results.append({
                "name": name,
                "prices": prices,
                "changes": {
                    platform: meta[name][platform]
                    for platform in prices
                    if isinstance(meta[name].get(platform), dict)
                },
                "lowest_platform": lowest_platform,
                "lowest_price": prices[lowest_platform],
                "lowest_change": low_change.get("change"),
                "lowest_change_pct": low_change.get("change_pct"),
                "lowest_shop": meta[name].get("lowest_shop"),
                "lowest_model": meta[name].get("lowest_model"),
            })

        # 平台列顺序固定，前端切换日期时列不会跳来跳去
        order = ["jd", "taobao", "pdd", "douyin", "xianyu"]
        return {
            "date": target.isoformat(),
            "category": category,
            "condition": condition,
            "platforms": [p for p in order if p in platforms],
            "products": results,
        }
    finally:
        db.close()


@app.get("/api/digest")
def get_digest_api(day: str = ""):
    """某天的行情归纳（视频旁白），没有归档时返回空对象"""
    target = None
    if day:
        try:
            target = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            return {"error": f"日期格式不对：{day}，应为 YYYY-MM-DD"}

    digest = repository.get_digest(target)
    if not digest:
        return {}
    return {
        "date": digest["date"].isoformat(),
        "title": digest["title"],
        "market_summary": digest["market_summary"],
        "news": digest["news"],
    }


@app.get("/api/news")
def list_news(keyword: str = "", days: int = 7):
    """最近的硬件新闻，按标题去重"""
    return {"news": repository.search_news(keyword=keyword, days=days)}


@app.get("/api/reports")
def list_reports():
    """视频总结报告列表"""
    if not REPORTS_DIR.exists():
        return {"reports": []}
    files = sorted(REPORTS_DIR.glob("bilibili_summary_*.md"), reverse=True)
    return {"reports": [{"name": f.name, "content": f.read_text(encoding="utf-8")} for f in files[:5]]}


@app.post("/api/collect")
def collect(bg: BackgroundTasks, videos: int = 2):
    """后台触发 B 站视频采集（耗时较长，放后台执行）"""
    from app.crawler.bilibili_pipeline import BilibiliPipeline

    def _run():
        BilibiliPipeline().run(max_videos=videos)

    bg.add_task(_run)
    return {"status": "started", "message": f"开始采集最近 {videos} 个视频，稍后查看 /api/reports"}


@app.get("/health")
def health():
    return {"status": "ok"}


# 挂载静态文件（必须放在所有路由之后，否则会拦截 API 请求）
# 用绝对路径，避免从其他目录启动 uvicorn 时找不到 app/web
WEB_DIR = Path(__file__).resolve().parent / "web"
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
