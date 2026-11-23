"""
B站视频价格采集流水线：

1. 获取硬件UP主的视频列表（截至指定日期）
2. 获取字幕/简介/评论
3. 每隔 N 秒提取视频快照（B站 videoshot）
4. 视觉模型分析截图 + 字幕 → 输出视频总结
5. LLM 提取显卡价格 → 异常过滤 → 入库
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func

from app.crawler.bilibili_crawler import (
    BilibiliCrawler,
    HARDWARE_UPLOADERS,
    LoginRequiredError,
    dedupe_frames,
)
from app.crawler.audio_transcriber import transcribe, transcribe_video, segments_to_text
from app.crawler.bilibili_price_extractor import extract_prices
from app.crawler.digest_extractor import extract_digest
from app.crawler.video_summarizer import VisionServiceError, summarize_video
from app.crawler.anomaly_detector import (
    load_benchmark,
    load_used_benchmark,
    check_anomaly,
    save_benchmark_from_prices,
)
from app.db.database import Base, SessionLocal, engine
from app.db.models import PriceHistory, Product, TranscriptSegment, VideoDigest
from app.db.repository import save_price, save_digest, save_transcript
from app.core.logger import logger

# 视频总结输出目录
REPORT_DIR = Path(__file__).resolve().parents[2] / "reports"
# 截图输出目录
SNAPSHOT_DIR = Path(__file__).resolve().parents[2] / "snapshots"
# 音频输出目录
AUDIO_DIR = Path(__file__).resolve().parents[2] / "audio"
# 只处理标题含该关键词的视频（日报类才有完整价格表）
TITLE_KEYWORD = "显卡日报"
# 单日达到这个条数才算真实日报，用于区间采集时判断哪些日期不用重跑。
# seed 生成的占位历史每天 132 条，真实日报在 180 条以上。
REAL_DAY_MIN_ROWS = 150

# 各品类单日最低条数：真实日报大约显卡 100（全新 60 + 二手 25~39）、
# CPU 60、内存 18、硬盘 12。阈值留了余量，只在明显缺一整类时告警。
CATEGORY_MIN_ROWS = {"gpu": 80, "cpu": 50, "memory": 12, "ssd": 8}
CATEGORY_NAMES = {"gpu": "显卡", "cpu": "CPU", "memory": "内存", "ssd": "硬盘"}

# 已知缺口清单：记录反复采集仍不完整的日期，避免每轮都把同一批日期重跑一遍。
# 存在 app/data 下的 JSON 里而不是数据库，是因为它属于采集过程中的运行状态，
# 不进业务数据，也不该被 API 查询到。
KNOWN_GAPS_FILE = Path(__file__).resolve().parents[1] / "data" / "known_gaps.json"
# 同一天连续失败达到这个次数后判定为已知缺口
MAX_GAP_ATTEMPTS = 3


def load_known_gaps() -> dict:
    """读取缺口清单，格式 {日期: {attempts, reason, last_try, confirmed}}"""
    if not KNOWN_GAPS_FILE.exists():
        return {}
    try:
        return json.loads(KNOWN_GAPS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # 文件损坏时按空处理，不能让排障数据反过来阻塞采集
        logger.warning(f"[bili-pipeline] 缺口清单读取失败，按空处理: {KNOWN_GAPS_FILE}")
        return {}


def _save_known_gaps(gaps: dict) -> None:
    KNOWN_GAPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    KNOWN_GAPS_FILE.write_text(
        json.dumps(gaps, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def record_gap_attempt(day: str, reason: str) -> dict:
    """
    记录某天一次采集不完整

    :param day: 日期字符串 yyyy-MM-dd
    :param reason: 不完整的原因，用于事后核对
    :return: 更新后的记录，confirmed 表示已达阈值
    """
    gaps = load_known_gaps()
    item = gaps.setdefault(day, {"attempts": 0})
    item["attempts"] = int(item.get("attempts", 0)) + 1
    item["reason"] = reason
    item["last_try"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    item["confirmed"] = item["attempts"] >= MAX_GAP_ATTEMPTS
    _save_known_gaps(gaps)
    return item


def clear_gap(day: str) -> None:
    """某天补齐后从缺口清单移除，避免残留记录让它以后被误跳过"""
    gaps = load_known_gaps()
    if gaps.pop(day, None) is not None:
        _save_known_gaps(gaps)



def check_day_coverage(day) -> list:
    """
    检查某天各品类条数是否低于预期

    :return: 不足项描述列表，空列表表示没发现问题
    """
    db = SessionLocal()
    try:
        rows = (
            db.query(Product.category, func.count(PriceHistory.id))
            .join(Product, Product.id == PriceHistory.product_id)
            .filter(PriceHistory.date == day)
            .group_by(Product.category)
            .all()
        )
    finally:
        db.close()

    counts = dict(rows)
    problems = []
    for category, minimum in CATEGORY_MIN_ROWS.items():
        count = counts.get(category, 0)
        if count < minimum:
            problems.append(f"{CATEGORY_NAMES[category]} {count}/{minimum}")
    return problems


def _fmt_change(change) -> str:
    """涨跌金额格式化：无前一日数据留空，0 显示持平"""
    if change is None:
        return "-"
    if change == 0:
        return "持平"
    return f"{change:+.0f}"


def _fmt_pct(pct) -> str:
    """涨跌幅格式化"""
    if pct is None:
        return "-"
    if pct == 0:
        return "0%"
    return f"{pct:+.2f}%"


class BilibiliPipeline:
    """B站视频价格采集流水线"""

    def __init__(self, frame_interval: int = 3, save_frames: bool = True,
                 use_asr: bool = True, check_anomalies: bool = True):
        """
        :param frame_interval: 抽帧间隔(秒)，默认每 3 秒一帧
        :param save_frames: 是否把帧保存到 snapshots/ 目录
        :param use_asr: 是否把音频转写成文本。关闭后文本只取视频简介，省掉转写时间
        :param check_anomalies: 是否做异常过滤。补基准期这一期必须关掉：
                                建基准就是为了拿到真实行情，不能先用旧基准把它筛掉
        """
        self.crawler = BilibiliCrawler()
        self.benchmark = load_benchmark()
        self.used_benchmark = load_used_benchmark()
        self.frame_interval = frame_interval
        self.save_frames = save_frames
        self.use_asr = use_asr
        self.check_anomalies = check_anomalies
        # 老库没有 video_digests 表时自动补建，避免必须先跑一次 seed_data
        Base.metadata.create_all(bind=engine)

    def process_video(self, video: dict) -> dict:
        """
        处理单个视频：字幕 + 快照 → 总结 + 价格

        :param video: get_user_videos 返回的视频信息
        :return: {"bvid", "title", "summary", "visual", "prices", "error"}
        """
        bvid = video["bvid"]
        title = video["title"]
        result = {
            "bvid": bvid,
            "title": title,
            "pubdate": video.get("pubdate"),
            "summary": "",
            "visual": "",
            "digest": {"market_summary": "", "news": []},
            "prices": [],
            "error": None,
        }

        # 1. 获取视频详情
        detail = self.crawler.get_video_detail(bvid)
        if not detail:
            result["error"] = "获取视频详情失败"
            return result
        cid = detail.get("cid")
        pubdate = video.get("pubdate")
        record_date = pubdate.date() if isinstance(pubdate, datetime) else None

        # 2. 音频转写与抽帧互不依赖，放进线程池并行，总耗时约等于较慢的那个
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=2) as pool:
            asr_future = (
                pool.submit(transcribe_video, self.crawler, bvid, cid, AUDIO_DIR)
                if self.use_asr else None
            )
            frames_future = pool.submit(self._extract_frames, bvid, cid)
            transcript_segments = (asr_future.result() if asr_future else []) or []
            transcript = segments_to_text(transcript_segments)
            snapshots = frames_future.result()
        logger.info(
            f"[bili-pipeline] {bvid} 转写+抽帧耗时 {time.perf_counter() - started:.1f}s，"
            f"去重后保留 {len(snapshots)} 帧"
        )
        if not snapshots:
            # 没有原画质截图就没有可信的价格来源，本期直接放弃，避免错价入库
            result["error"] = "ffmpeg 抽帧失败，未提取价格"
            return result

        # 转写和截图互不依赖，先把原话入库：后面摘要或价格抽取失败时，文字也不会丢
        if record_date and transcript_segments:
            saved = save_transcript(bvid, record_date, transcript_segments)
            if not saved:
                logger.warning(f"[bili-pipeline] {bvid} 转写分段未入库")

        # 转写为空时只用简介兜底。评论内容经常和视频主题无关，不参与总结。
        text_for_summary = transcript or (detail.get("desc") or "")
        if self.use_asr and not transcript:
            logger.info(f"[bili-pipeline] {bvid} 音频转写为空，仅用简介作为文本输入")

        # 3. 视觉模型总结（截图 + 字幕）
        try:
            summary_data = summarize_video(title, text_for_summary, snapshots)
            result["summary"] = summary_data["summary"]
            result["visual"] = summary_data["visual"]
        except VisionServiceError:
            # 服务级故障必须冒泡：吞掉会让这一期被当成「没有价格表」记进缺口清单
            raise
        except Exception as e:
            logger.error(f"[bili-pipeline] 视频总结失败({bvid}): {e}")
            result["error"] = f"总结失败: {e}"

        # 3.5 旁白拆成「当日价格归纳」和「硬件新闻」，入库供以后按日期/关键词查询。
        # 这两块只来自旁白，不依赖截图，所以放在价格抽取之前。
        digest = extract_digest(title, transcript)
        result["digest"] = digest
        if record_date and (digest["market_summary"] or digest["news"]):
            save_digest(bvid, record_date, title, digest["market_summary"], digest["news"])

        # 4. 从截图分析文本 + 字幕中提取价格
        extract_source = "\n\n".join(x for x in [result["visual"], transcript] if x)
        if extract_source:
            prices = extract_prices(
                extract_source,
                new_names=list(self.benchmark.keys()),
                used_names=list(self.used_benchmark.keys()),
            )
            valid = []
            seen = {}  # (型号, 平台, 成色) -> 价格，用于发现同一期内的重复型号
            for p in prices:
                name = p.get("name", "")
                platform = p.get("platform", "unknown")
                price = p.get("price", 0)
                category = p.get("category", "")
                condition = p.get("condition", "used" if platform == "xianyu" else "new")
                if not name or not price:
                    continue
                # 二手价和全新价分开比对，否则正常二手价会被判成"偏低异常"
                benchmark = self.used_benchmark if condition == "used" else self.benchmark
                # 硬件型号是固定目录，只接受与基准表完全一致的名称。
                # 截图漏字（4080 S 读成 4080）这类错误直接跳过，不做模糊猜测，
                # 避免把价格串到别的型号上。
                if name not in benchmark:
                    logger.warning(
                        f"[bili-pipeline] 型号不在目录中，跳过: {name} @ {platform} = {price}"
                    )
                    continue
                # 传入视频自带的涨跌幅，让检测能区分"读错价格"和"当天真实大涨"
                # 建基准的那一期必须跳过检测，否则新行情会被旧基准整批筛掉
                if self.check_anomalies:
                    is_anomaly, reason = check_anomaly(
                        name, platform, price, benchmark, change=p.get("change")
                    )
                    if is_anomaly:
                        logger.info(f"[bili-pipeline] 排除异常: {name} @ {platform}={price}, {reason}")
                        continue
                # 同一期里同型号同平台出现两个价，几乎都是截图漏掉了型号后缀
                # (如 4080 S 读成 4080)。后入库会覆盖前一条，直接拦下并告警。
                key = (name, platform, condition)
                if key in seen:
                    logger.warning(
                        f"[bili-pipeline] 同型号重复: {name} @ {platform}({condition}) "
                        f"{seen[key]} / {price}，疑似漏识别后缀，跳过后者"
                    )
                    continue
                seen[key] = price
                # 数据日期取视频发布日期，补跑往期视频时不会把旧价写到今天名下
                saved = save_price(name, platform, price, category=category,
                                   condition=condition,
                                   change=p.get("change"),
                                   change_pct=p.get("change_pct"),
                                   record_date=record_date)
                valid.append({"name": name, "category": category, "condition": condition,
                              "platform": platform, "price": price,
                              "change": saved["change"], "change_pct": saved["change_pct"],
                              "alert": saved["alert"]})
            result["prices"] = valid

        return result

    def _extract_frames(self, bvid: str, cid: int) -> list:
        """
        抽帧并去重

        只用 ffmpeg 抽原画质帧。失败时重试一次，仍失败就返回空列表，
        不退回 B站 videoshot 缩略图：缩略图只有 360px，视觉模型会把
        15999 读成 8999、41999 读成 16999，错价还会覆盖库里的正确数据。
        宁可这一期不采价格，也不能写错。
        """
        out_dir = str(SNAPSHOT_DIR / f"{bvid}_{self.frame_interval}s") if self.save_frames else None
        for attempt in (1, 2, 3):
            frames = self.crawler.get_video_frames(
                bvid, cid, interval=self.frame_interval, output_dir=out_dir
            )
            if frames:
                return dedupe_frames(frames)
            if attempt < 3:
                # 家里网络到 B站 CDN 不稳定，隔 5 秒再试，给连接恢复留时间
                logger.warning(f"[bili-pipeline] {bvid} ffmpeg 抽帧失败，5 秒后第 {attempt + 1} 次尝试")
                time.sleep(5)
        logger.error(f"[bili-pipeline] {bvid} ffmpeg 抽帧 3 次失败，本期不提取价格")
        return []

    def run(self, uid: str = None, max_videos: int = 3,
            before_date: datetime = None) -> list:
        """
        执行完整采集流程

        :param uid: UP主UID，默认芝士华莱士
        :param max_videos: 最多处理多少个视频
        :param before_date: 只处理此日期之前的视频
        :return: 每个视频的处理结果
        """
        if uid is None:
            uid = HARDWARE_UPLOADERS["芝士华莱士"]

        logger.info(f"[bili-pipeline] 开始采集 UP主={uid}, 最多{max_videos}个视频")
        # 多取一些候选，过滤掉非日报类视频后再截取
        candidates = self.crawler.get_user_videos(
            uid, max_count=max_videos * 5, before_date=before_date
        )
        videos = [v for v in candidates if TITLE_KEYWORD in v["title"]]
        skipped_titles = [v["title"] for v in candidates if TITLE_KEYWORD not in v["title"]]
        if skipped_titles:
            logger.info(f"[bili-pipeline] 标题不含'{TITLE_KEYWORD}'，跳过 {len(skipped_titles)} 个视频")
        videos = videos[:max_videos]
        if not videos:
            logger.warning(f"[bili-pipeline] 未找到标题含'{TITLE_KEYWORD}'的视频")
            return []

        # 按发布日期分组，同一天可能有多个视频
        by_day = {}
        for v in videos:
            day = v["pubdate"].strftime("%Y-%m-%d")
            by_day.setdefault(day, []).append(v)

        results = []
        for day, day_videos in by_day.items():
            day_results = []
            for v in day_videos:
                logger.info(f"[bili-pipeline] 处理: {v['title'][:40]} ({day})")
                day_results.append(self.process_video(v))

            # 这一天所有视频都没提取到价格，标记跳过
            if not any(r.get("prices") for r in day_results):
                logger.warning(f"[bili-pipeline] {day} 全部视频无有效价格数据，跳过该天")
                for r in day_results:
                    r["skipped"] = True
            else:
                problems = check_day_coverage(datetime.strptime(day, "%Y-%m-%d").date())
                if problems:
                    logger.warning(f"[bili-pipeline] {day} 品类不全: {', '.join(problems)}")
            results.extend(day_results)

        # 写总结报告
        self._write_report(results)
        return results

    def _roll_benchmark(self, prices: list) -> None:
        """
        把当天提取到的价格并入内存基准，供下一天做异常比对

        区间补采跨半年，价格本来就会涨跌几十个百分点。如果整段都拿 5-01
        的固定值去比，后面的真实行情会被判成异常整批丢掉。滚动基准既保留
        5-01 作为起点，又能让阈值始终对应"上一天"，只用来抓读错价格。
        只改内存，不回写文件，基准文件的日期仍是 2026-05-01。
        """
        for item in prices:
            name = item.get("name")
            platform = item.get("platform")
            price = item.get("price")
            if not name or not platform or not price:
                continue
            target = self.used_benchmark if item.get("condition") == "used" else self.benchmark
            info = target.setdefault(name, {"prices": {}})
            info.setdefault("prices", {})[platform] = price

    def run_range(self, start_date: datetime, end_date: datetime = None,
                  uid: str = None, max_videos: int = None,
                  skip_existing: bool = True,
                  retry_gaps: bool = False) -> list:
        """
        按发布日期从旧到新逐期采集，用于补齐历史数据

        和 run() 的区别：run() 只取最新 N 个；这里给定起始日期，把区间内的
        日报按时间顺序跑完。已经入库的日期直接跳过，中断后重跑能接着采。

        :param start_date: 起始日期（含当天）
        :param end_date: 结束日期（含当天），不传则到今天
        :param max_videos: 最多处理多少期，不传则区间内全部
        :param skip_existing: 跳过价格历史里已有数据的日期
        :param retry_gaps: 忽略已知缺口清单，重新尝试之前多次失败的日期
        """
        if uid is None:
            uid = HARDWARE_UPLOADERS["芝士华莱士"]
        if end_date is None:
            end_date = datetime.now()

        logger.info(f"[bili-pipeline] 区间采集: {start_date.date()} ~ {end_date.date()}")
        # 一次拉全列表再按日期过滤：UP 主目前 900 多个视频，每页 30 个
        candidates = self.crawler.get_user_videos(uid, max_count=1500)
        videos = [
            v for v in candidates
            if TITLE_KEYWORD in v["title"]
            and start_date.date() <= v["pubdate"].date() <= end_date.date()
        ]
        videos.sort(key=lambda v: v["pubdate"])

        # 已达重试上限的日期不再消耗视觉模型调用，只在结尾汇总告警
        known_gaps = load_known_gaps()
        confirmed_gaps = {
            day: info for day, info in known_gaps.items() if info.get("confirmed")
        }

        if skip_existing:
            db = SessionLocal()
            try:
                coverage_rows = (
                    db.query(PriceHistory.date, Product.category,
                             func.count(PriceHistory.id))
                    .join(Product, Product.id == PriceHistory.product_id)
                    .filter(PriceHistory.date >= start_date.date(),
                            PriceHistory.date <= end_date.date())
                    .group_by(PriceHistory.date, Product.category)
                    .all()
                )
            finally:
                db.close()

            by_day = {}
            for day, category, count in coverage_rows:
                by_day.setdefault(day, {})[category] = count

            existing = set()
            partial = []
            for day, counts in by_day.items():
                total = sum(counts.values())
                problems = [
                    f"{CATEGORY_NAMES[category]} {counts.get(category, 0)}/{minimum}"
                    for category, minimum in CATEGORY_MIN_ROWS.items()
                    if counts.get(category, 0) < minimum
                ]
                # 条数明显偏少时单独标记：可能是 seed 占位数据，也可能是采集中断。
                # 之前这里直接 continue，导致这类日期既不跳过也不告警，每轮都被重采
                if total < REAL_DAY_MIN_ROWS:
                    problems.insert(0, f"总条数 {total}/{REAL_DAY_MIN_ROWS}")
                if problems:
                    partial.append((day, problems))
                else:
                    existing.add(day)

            if existing:
                logger.info(f"[bili-pipeline] 已有完整数据 {len(existing)} 天，跳过这些日期")
            if partial:
                logger.warning(
                    f"[bili-pipeline] {len(partial)} 天品类不全，本次会重采: "
                    + "；".join(f"{day}({', '.join(problems)})"
                               for day, problems in sorted(partial))
                )
            videos = [v for v in videos if v["pubdate"].date() not in existing]

        # 已知缺口不再重采。这类日期通常是视频本身缺某个品类（如没有 CPU 表），
        # 重跑多少次结果都一样，继续跑只是白烧视觉模型调用
        if confirmed_gaps and not retry_gaps:
            in_range = [
                v for v in videos
                if v["pubdate"].strftime("%Y-%m-%d") in confirmed_gaps
            ]
            videos = [
                v for v in videos
                if v["pubdate"].strftime("%Y-%m-%d") not in confirmed_gaps
            ]
            if in_range:
                skipped_days = sorted({v["pubdate"].strftime("%Y-%m-%d") for v in in_range})
                logger.warning(
                    f"[bili-pipeline] 跳过 {len(skipped_days)} 个已知缺口日期"
                    f"（已重试满 {MAX_GAP_ATTEMPTS} 次），如需强制重试加 --retry-gaps"
                )
                for day in skipped_days:
                    logger.warning(
                        f"[bili-pipeline]   已知缺口 {day}: "
                        f"{confirmed_gaps[day].get('reason', '原因未记录')}"
                    )

        if max_videos:
            videos = videos[:max_videos]
        if not videos:
            logger.info("[bili-pipeline] 区间内没有需要采集的视频")
            return []

        by_day = {}
        for v in videos:
            day = v["pubdate"].strftime("%Y-%m-%d")
            by_day.setdefault(day, []).append(v)

        results = []
        total = len(videos)
        done = 0
        for day in sorted(by_day):
            day_results = []
            for v in by_day[day]:
                done += 1
                logger.info(
                    f"[bili-pipeline] 第 {done}/{total} 期: {v['title'][:40]} ({day})"
                )
                day_results.append(self.process_video(v))

            if not any(r.get("prices") for r in day_results):
                logger.warning(f"[bili-pipeline] {day} 全部视频无有效价格数据，跳过该天")
                for r in day_results:
                    r["skipped"] = True
                if all(r.get("error") for r in day_results):
                    # 所有视频都跑失败了（CDN 连不上、抽帧失败等），属于采集环境问题。
                    # 这种失败重试几次可能就好了，记成缺口会让这一天在若干轮后被永久跳过，
                    # 所以只告警不计数，留待下次采集重试
                    logger.warning(
                        f"[bili-pipeline] {day} 失败原因是采集环境，未计入缺口清单: "
                        f"{day_results[0].get('error')}"
                    )
                else:
                    item = record_gap_attempt(day, "整期无有效价格数据")
                    if item["confirmed"]:
                        logger.warning(
                            f"[bili-pipeline] {day} 已连续 {item['attempts']} 次无数据，"
                            "记为已知缺口，后续采集自动跳过"
                        )
            else:
                # 当天入库成功才滚动基准，失败的一天不能污染下一天的参照
                self._roll_benchmark([p for r in day_results for p in r.get("prices", [])])
                problems = check_day_coverage(datetime.strptime(day, "%Y-%m-%d").date())
                if problems:
                    logger.warning(f"[bili-pipeline] {day} 品类不全: {', '.join(problems)}")
                    item = record_gap_attempt(day, ", ".join(problems))
                    if item["confirmed"]:
                        logger.warning(
                            f"[bili-pipeline] {day} 已连续 {item['attempts']} 次品类不全，"
                            "记为已知缺口，后续采集自动跳过"
                        )
                else:
                    # 补齐后清掉历史记录，否则它会一直被当成待补日期
                    clear_gap(day)
            results.extend(day_results)

        processed_days = {r["pubdate"].date() for r in results if r.get("pubdate")}
        self._log_coverage_summary(processed_days)
        self._write_report(results)
        return results

    def backfill_transcripts(self, start_date: datetime, end_date: datetime = None) -> int:
        """
        只补录缺失的 ASR 转写，不抽帧、不调视觉模型

        已经跑过摘要和价格的日期，重新走完整流程会重复消耗视觉调用；
        这里只用缓存音频重新转写，给历史数据补原文。
        """
        if end_date is None:
            end_date = datetime.now()

        db = SessionLocal()
        try:
            digests = (
                db.query(VideoDigest.bvid, VideoDigest.video_date)
                .filter(VideoDigest.video_date >= start_date.date(),
                        VideoDigest.video_date <= end_date.date())
                .order_by(VideoDigest.video_date)
                .all()
            )
            existing = {
                row[0] for row in db.query(TranscriptSegment.bvid).distinct().all()
            }
        finally:
            db.close()

        targets = [(bvid, day) for bvid, day in digests if bvid not in existing]
        logger.info(f"[bili-pipeline] 待补转写 {len(targets)} 期")
        saved = 0
        for index, (bvid, day) in enumerate(targets, 1):
            audio_path = AUDIO_DIR / f"{bvid}.wav"
            segments = []
            if audio_path.exists() and audio_path.stat().st_size > 0:
                segments = transcribe(audio_path)
            if not segments:
                # 缓存缺失或损坏时重新下载，避免历史数据因为没有音频而漏掉
                detail = self.crawler.get_video_detail(bvid)
                cid = detail.get("cid") if detail else None
                segments = (
                    transcribe_video(self.crawler, bvid, cid, AUDIO_DIR) if cid else []
                )
            if save_transcript(bvid, day, segments):
                saved += 1
            logger.info(f"[bili-pipeline] 转写补录 {index}/{len(targets)}: {bvid} ({day})")

        logger.info(f"[bili-pipeline] 转写补录完成: {saved}/{len(targets)} 期")
        return saved

    @staticmethod
    def _log_coverage_summary(days) -> None:
        """区间跑完后汇总品类不全的日期，方便决定是否补采"""
        if not days:
            return

        db = SessionLocal()
        try:
            rows = (
                db.query(PriceHistory.date, Product.category,
                         func.count(PriceHistory.id))
                .join(Product, Product.id == PriceHistory.product_id)
                .filter(PriceHistory.date.in_(sorted(days)))
                .group_by(PriceHistory.date, Product.category)
                .all()
            )
        finally:
            db.close()

        by_day = {}
        for day, category, count in rows:
            by_day.setdefault(day, {})[category] = count

        incomplete = []
        for day in sorted(days):
            counts = by_day.get(day, {})
            problems = [
                f"{CATEGORY_NAMES[category]} {counts.get(category, 0)}/{minimum}"
                for category, minimum in CATEGORY_MIN_ROWS.items()
                if counts.get(category, 0) < minimum
            ]
            if problems:
                incomplete.append((day, problems))

        if incomplete:
            logger.warning(f"[bili-pipeline] 区间内 {len(incomplete)} 天品类不全：")
            for day, problems in incomplete:
                logger.warning(f"[bili-pipeline]   {day}: {', '.join(problems)}")
        else:
            logger.info("[bili-pipeline] 区间内各天品类条数均达标")

    @staticmethod
    def _write_report(results: list) -> str:
        """把视频总结写成 Markdown 报告"""
        if not results:
            return ""
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        date_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = REPORT_DIR / f"bilibili_summary_{date_tag}.md"

        lines = [f"# B站硬件视频总结 ({datetime.now().strftime('%Y-%m-%d %H:%M')})", ""]
        for r in results:
            lines.append(f"## {r['title']}")
            pub = r["pubdate"].strftime("%Y-%m-%d") if r.get("pubdate") else "未知"
            lines.append(f"- 发布日期：{pub}")
            lines.append(f"- 视频链接：https://www.bilibili.com/video/{r['bvid']}")
            if r.get("error"):
                lines.append(f"- 错误：{r['error']}")
            if r.get("skipped"):
                lines.append("- 状态：当天无有效价格数据，已跳过")
            lines.append("")
            if r.get("summary"):
                lines.append("### 视频总结")
                lines.append(r["summary"])
                lines.append("")
            digest = r.get("digest") or {}
            if digest.get("market_summary"):
                lines.append("### 当日价格归纳")
                lines.append(digest["market_summary"])
                lines.append("")
            if digest.get("news"):
                lines.append("### 硬件新闻")
                for item in digest["news"]:
                    lines.append(f"- **{item['title']}**：{item['detail']}")
                lines.append("")
            if r.get("visual"):
                lines.append("### 截图识别结果")
                visual = r["visual"]
                lines.append(visual[:3000] + ("..." if len(visual) > 3000 else ""))
                lines.append("")
            if r.get("prices"):
                lines.append("### 提取到的价格")
                lines.append("| 产品 | 品类 | 成色 | 平台 | 价格 | 较前日 | 涨跌幅 |")
                lines.append("|---|---|---|---|---|---|---|")
                for p in r["prices"]:
                    lines.append(f"| {p['name']} | {p.get('category', '')} | "
                                 f"{p.get('condition', '')} | {p['platform']} | {p['price']} | "
                                 f"{_fmt_change(p.get('change'))} | "
                                 f"{_fmt_pct(p.get('change_pct'))} |")
                lines.append("")
            lines.append("---")
            lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"[bili-pipeline] 总结报告已写入: {path}")
        return str(path)


def run_bilibili_collection(max_videos: int = 3, before_date: datetime = None,
                            frame_interval: int = 3, use_asr: bool = True):
    """入口函数：执行B站视频采集 + 总结"""
    pipeline = BilibiliPipeline(frame_interval=frame_interval, use_asr=use_asr)
    return pipeline.run(max_videos=max_videos, before_date=before_date)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="B站硬件视频采集与总结")
    parser.add_argument("--uid", default=None, help="UP主UID，默认芝士华莱士")
    parser.add_argument("--videos", type=int, default=None,
                        help="处理视频数量；区间模式不填表示区间内全部")
    parser.add_argument("--interval", type=int, default=3, help="抽帧间隔(秒)，默认3")
    parser.add_argument("--before", default=None, help="只处理此日期之前的视频，格式 2026-01-01")
    parser.add_argument("--from", dest="from_date", default=None,
                        help="区间采集起始日期，格式 2026-05-01，按时间从旧到新跑")
    parser.add_argument("--recent", type=int, default=None,
                        help="日常用：只扫最近 N 天，自动跳过已入库和已知缺口，例如 --recent 3")
    parser.add_argument("--to", dest="to_date", default=None,
                        help="区间采集结束日期（含当天），默认今天")
    parser.add_argument("--include-existing", action="store_true",
                        help="区间模式不跳过已入库日期（默认跳过，便于中断续跑）")
    parser.add_argument("--retry-gaps", action="store_true",
                        help=f"忽略已知缺口清单，重新尝试之前失败满 {MAX_GAP_ATTEMPTS} 次的日期")
    parser.add_argument("--bootstrap-benchmark", action="store_true",
                        help="把本期提取的价格写成新基准表（自动关闭异常过滤）")
    parser.add_argument("--no-asr", action="store_true",
                        help="关闭音频转写，只用视频简介作为文本（更快）")
    parser.add_argument("--transcripts-only", action="store_true",
                        help="只补录缺失的 ASR 转写（配合 --from），不抽帧、不调视觉模型")
    args = parser.parse_args()

    def _parse_day(value):
        return datetime.strptime(value, "%Y-%m-%d") if value else None

    before = _parse_day(args.before)
    start = _parse_day(args.from_date)
    end = _parse_day(args.to_date)

    if args.recent:
        # 日常增量采集：只把窗口内还没入库的日期补齐。
        # 窗口给宽一点没有代价，已有数据的日期会被跳过
        if start:
            parser.error("--recent 和 --from 不能同时使用")
        if args.bootstrap_benchmark:
            parser.error("--recent 不能配合 --bootstrap-benchmark 使用")
        end = end or datetime.now()
        start = end - timedelta(days=args.recent)

    if args.bootstrap_benchmark and not start:
        parser.error("--bootstrap-benchmark 需要配合 --from 指定基准日期")

    pipeline = BilibiliPipeline(
        frame_interval=args.interval,
        use_asr=not args.no_asr,
        check_anomalies=not args.bootstrap_benchmark,
    )

    if args.transcripts_only:
        if not start:
            parser.error("--transcripts-only 需要配合 --from 指定日期")
        saved = pipeline.backfill_transcripts(start, end)
        print(f"\n转写补录完成: {saved} 期")
        raise SystemExit(0)

    if start:
        max_videos = args.videos
        if args.bootstrap_benchmark and max_videos is None:
            # 建基准只需要区间第一期，避免误把整个区间重跑一遍
            max_videos = 1
        results = pipeline.run_range(
            start_date=start, end_date=end, uid=args.uid,
            max_videos=max_videos,
            skip_existing=not args.include_existing and not args.bootstrap_benchmark,
            retry_gaps=args.retry_gaps,
        )
    else:
        results = pipeline.run(uid=args.uid, max_videos=args.videos or 3,
                               before_date=before)

    if args.bootstrap_benchmark:
        all_prices = [p for r in results for p in r.get("prices", [])]
        path = save_benchmark_from_prices(
            all_prices, start.strftime("%Y-%m-%d"),
            source=results[0]["title"] if results else "",
        )
        print(f"\n基准表已更新: {path}，写入 {len(all_prices)} 条价格")

    print(f"\n处理完成，共 {len(results)} 个视频:")
    for r in results:
        print(f"  - {r['title'][:40]} | 价格 {len(r['prices'])} 条 | "
              f"{'失败: ' + r['error'] if r.get('error') else '成功'}")


if __name__ == "__main__":
    try:
        main()
    except LoginRequiredError as exc:
        # 登录问题不是程序缺陷，用简洁提示代替 traceback，方便直接照做
        print(f"\n[登录失败] {exc}")
        raise SystemExit(1)
    except VisionServiceError as exc:
        # 欠费、鉴权这类问题重跑不会成功。明确提示并说明未写入缺口，
        # 避免用户以为要补数据，也避免这几天被误标成「视频本身没价格」
        print(f"\n[视觉模型不可用] {exc}")
        print("本次采集中止，相关日期未记入缺口清单。充值或修复密钥后重新运行即可。")
        raise SystemExit(1)
