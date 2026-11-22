"""仓储层测试：入库幂等、涨跌默认值、品类推断、品类覆盖校验"""
import datetime as dt

from app.crawler.bilibili_pipeline import check_day_coverage
from app.db.models import PriceHistory, Product
from app.db.repository import _infer_category, save_price

DAY = dt.date(2026, 11, 19)


def test_save_price_creates_product(db):
    """首次入库会同时建产品和价格记录"""
    save_price("RTX 5090 D", "jd", 43999, category="gpu", record_date=DAY)

    product = db.query(Product).filter_by(name="RTX 5090 D", platform="jd").one()
    assert product.current_price == 43999
    assert product.category == "gpu"

    history = db.query(PriceHistory).filter_by(product_id=product.id).all()
    assert len(history) == 1
    assert history[0].date == DAY


def test_save_price_same_day_overwrites(db):
    """同一天同一产品同一平台重复抓取，只保留一条并更新价格"""
    save_price("RTX 5090 D", "jd", 43999, category="gpu", record_date=DAY)
    save_price("RTX 5090 D", "jd", 42999, category="gpu", record_date=DAY)

    product = db.query(Product).filter_by(name="RTX 5090 D", platform="jd").one()
    history = db.query(PriceHistory).filter_by(product_id=product.id).all()
    assert len(history) == 1
    assert history[0].price == 42999


def test_change_defaults_to_zero(db):
    """视频没标涨跌时，入库应为 0 而不是 NULL"""
    result = save_price(
        "RTX 5090 D", "jd", 43999, category="gpu",
        change=None, change_pct=None, record_date=DAY,
    )

    product = db.query(Product).filter_by(name="RTX 5090 D", platform="jd").one()
    assert product.change == 0.0
    assert product.change_pct == 0.0
    assert result["change"] == 0.0
    assert result["change_pct"] == 0.0


def test_new_and_used_price_are_separate(db):
    """全新和二手是同型号的两条独立记录，不能互相覆盖"""
    save_price("RTX 5090 D", "xianyu", 38000, category="gpu",
               condition="used", record_date=DAY)
    save_price("RTX 5090 D", "jd", 43999, category="gpu",
               condition="new", record_date=DAY)

    products = db.query(Product).filter_by(name="RTX 5090 D").all()
    assert len(products) == 2
    assert {p.condition for p in products} == {"new", "used"}


def test_backfill_does_not_overwrite_current_price(db):
    """补跑旧视频时，current_price 不能倒退成旧价格"""
    save_price("RTX 5090 D", "jd", 43999, category="gpu", record_date=DAY)
    save_price("RTX 5090 D", "jd", 41999, category="gpu",
               record_date=DAY - dt.timedelta(days=3))

    product = db.query(Product).filter_by(name="RTX 5090 D", platform="jd").one()
    assert product.current_price == 43999


def test_infer_category():
    """品类推断要能区分内存、硬盘、CPU、显卡"""
    assert _infer_category("DDR5 6000 16G") == "memory"
    assert _infer_category("SN850X NVMe 2T") == "ssd"
    assert _infer_category("i7-14700K") == "cpu"
    assert _infer_category("R7-9800X3D") == "cpu"
    assert _infer_category("RTX 5090 D") == "gpu"
    assert _infer_category("RX 9070 XT") == "gpu"


def test_check_day_coverage_reports_missing_categories(db):
    """只有显卡时，其余三个品类都应该被报缺"""
    save_price("RTX 5090 D", "jd", 43999, category="gpu", record_date=DAY)

    problems = check_day_coverage(DAY)
    assert "显卡 1/80" in problems
    assert "CPU 0/50" in problems
    assert "内存 0/12" in problems
    assert "硬盘 0/8" in problems


def test_check_day_coverage_empty_day(db):
    """当天完全没有数据时，四个品类全部报缺"""
    problems = check_day_coverage(dt.date(2020, 1, 1))
    assert len(problems) == 4
