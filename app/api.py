"""FastAPI 后端接口"""
from pathlib import Path

from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent.hardware_agent import agent
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

PLATFORM_NAMES = {"jd": "京东", "taobao": "淘宝", "pdd": "拼多多", "douyin": "抖音"}
REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports"


class ChatRequest(BaseModel):
    """用户请求的数据结构"""
    message: str


class ChatResponse(BaseModel):
    """返回给前端的数据结构"""
    reply: str


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    """对话接口：接收用户消息，返回 Agent 回答"""
    result = agent.invoke({"messages": [{"role": "user", "content": request.message}]})
    reply = result["messages"][-1].content
    return ChatResponse(reply=reply)


@app.get("/api/trend")
def get_trend(name: str, days: int = 7):
    """
    返回产品最近 N 天的价格趋势，分平台给出序列，供前端画多线对比图
    """
    db = SessionLocal()
    try:
        products = db.query(Product).filter(Product.name.contains(name)).all()
        if not products:
            return {"error": f"没有找到 {name}"}

        ids = [p.id for p in products]
        histories = (
            db.query(PriceHistory)
            .filter(PriceHistory.product_id.in_(ids))
            .order_by(PriceHistory.date.asc())
            .all()
        )

        # platform -> {日期: 价格}（同一天多条时取最后一条）
        by_platform = {}
        for h in histories:
            day = h.date.strftime("%m-%d")
            by_platform.setdefault(h.platform, {})[day] = h.price

        all_dates = sorted({d for m in by_platform.values() for d in m})[-days:]

        series = []
        for platform, price_map in by_platform.items():
            series.append({
                "platform": platform,
                "name": PLATFORM_NAMES.get(platform, platform),
                "prices": [price_map.get(d) for d in all_dates],
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
def list_products(category: str = ""):
    """产品列表（按型号聚合，给出各平台价格与最低价）"""
    db = SessionLocal()
    try:
        query = db.query(Product)
        if category:
            query = query.filter(Product.category == category)
        rows = query.all()
    finally:
        db.close()

    grouped = {}
    for p in rows:
        if p.current_price:
            grouped.setdefault(p.name, {})[p.platform] = p.current_price

    result = []
    for name, prices in sorted(grouped.items()):
        platform = min(prices, key=prices.get)
        result.append({
            "name": name,
            "prices": prices,
            "lowest_platform": platform,
            "lowest_price": prices[platform],
        })
    return {"products": result}


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
app.mount("/", StaticFiles(directory="app/web", html=True), name="web")
