"""
B站视频价格采集流水线：

1. 获取硬件UP主的视频列表（截至指定日期）
2. 获取字幕/简介/评论
3. 每隔 N 秒提取视频快照（B站 videoshot）
4. 视觉模型分析截图 + 字幕 → 输出视频总结
5. LLM 提取显卡价格 → 异常过滤 → 入库
"""
from datetime import datetime
from pathlib import Path

from app.crawler.bilibili_crawler import (
    BilibiliCrawler,
    HARDWARE_UPLOADERS,
    dedupe_frames,
)
from app.crawler.audio_transcriber import transcribe_video
from app.crawler.bilibili_price_extractor import extract_prices
from app.crawler.video_summarizer import summarize_video
from app.crawler.anomaly_detector import load_benchmark, check_anomaly
from app.db.repository import save_price
from app.core.logger import logger

# 视频总结输出目录
REPORT_DIR = Path(__file__).resolve().parents[2] / "reports"
# 截图输出目录
SNAPSHOT_DIR = Path(__file__).resolve().parents[2] / "snapshots"
# 音频输出目录
AUDIO_DIR = Path(__file__).resolve().parents[2] / "audio"
# 只处理标题含该关键词的视频（日报类才有完整价格表）
TITLE_KEYWORD = "显卡日报"


class BilibiliPipeline:
    """B站视频价格采集流水线"""

    def __init__(self, frame_interval: int = 3, save_frames: bool = True):
        """
        :param frame_interval: 抽帧间隔(秒)，默认每 3 秒一帧
        :param save_frames: 是否把帧保存到 snapshots/ 目录
        """
        self.crawler = BilibiliCrawler()
        self.benchmark = load_benchmark()
        self.frame_interval = frame_interval
        self.save_frames = save_frames

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
            "prices": [],
            "error": None,
        }

        # 1. 获取视频详情 + 音频转写
        detail = self.crawler.get_video_detail(bvid)
        if not detail:
            result["error"] = "获取视频详情失败"
            return result

        transcript = transcribe_video(self.crawler, bvid, detail.get("cid"), AUDIO_DIR)
        # 转写为空时只用简介兜底。评论内容经常和视频主题无关，不参与总结。
        text_for_summary = transcript or (detail.get("desc") or "")
        if not transcript:
            logger.info(f"[bili-pipeline] {bvid} 音频转写为空，仅用简介作为文本输入")

        # 2. 抽帧：优先 ffmpeg 按固定间隔抽帧，失败回退 videoshot 缩略图
        out_dir = str(SNAPSHOT_DIR / f"{bvid}_{self.frame_interval}s") if self.save_frames else None
        frames = self.crawler.get_video_frames(
            bvid, detail.get("cid"), interval=self.frame_interval, output_dir=out_dir
        )
        if not frames:
            logger.warning(f"[bili-pipeline] {bvid} ffmpeg 抽帧失败，回退 videoshot")
            frames = self.crawler.get_video_snapshots(
                bvid, detail.get("cid"), output_dir=out_dir, interval=self.frame_interval
            )
        # 去重：3秒一帧时大量帧内容相同，只保留有变化的
        snapshots = dedupe_frames(frames)

        # 3. 视觉模型总结（截图 + 字幕）
        try:
            summary_data = summarize_video(title, text_for_summary, snapshots)
            result["summary"] = summary_data["summary"]
            result["visual"] = summary_data["visual"]
        except Exception as e:
            logger.error(f"[bili-pipeline] 视频总结失败({bvid}): {e}")
            result["error"] = f"总结失败: {e}"

        # 4. 从截图分析文本 + 字幕中提取价格
        extract_source = "\n\n".join(x for x in [result["visual"], transcript] if x)
        if extract_source:
            prices = extract_prices(extract_source)
            valid = []
            for p in prices:
                name = p.get("name", "")
                platform = p.get("platform", "unknown")
                price = p.get("price", 0)
                if not name or not price:
                    continue
                is_anomaly, reason = check_anomaly(name, platform, price, self.benchmark)
                if is_anomaly:
                    logger.info(f"[bili-pipeline] 排除异常: {name} @ {platform}={price}, {reason}")
                    continue
                alert = save_price(name, platform, price, category="gpu")
                valid.append({"name": name, "platform": platform, "price": price, "alert": alert})
            result["prices"] = valid

        return result

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
            results.extend(day_results)

        # 写总结报告
        self._write_report(results)
        return results

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
            if r.get("visual"):
                lines.append("### 截图识别结果")
                visual = r["visual"]
                lines.append(visual[:3000] + ("..." if len(visual) > 3000 else ""))
                lines.append("")
            if r.get("prices"):
                lines.append("### 提取到的价格")
                lines.append("| 产品 | 平台 | 价格 |")
                lines.append("|---|---|---|")
                for p in r["prices"]:
                    lines.append(f"| {p['name']} | {p['platform']} | {p['price']} |")
                lines.append("")
            lines.append("---")
            lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"[bili-pipeline] 总结报告已写入: {path}")
        return str(path)


def run_bilibili_collection(max_videos: int = 3, before_date: datetime = None,
                            frame_interval: int = 3):
    """入口函数：执行B站视频采集 + 总结"""
    pipeline = BilibiliPipeline(frame_interval=frame_interval)
    return pipeline.run(max_videos=max_videos, before_date=before_date)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="B站硬件视频采集与总结")
    parser.add_argument("--uid", default=None, help="UP主UID，默认芝士华莱士")
    parser.add_argument("--videos", type=int, default=3, help="处理视频数量")
    parser.add_argument("--interval", type=int, default=3, help="抽帧间隔(秒)，默认3")
    parser.add_argument("--before", default=None, help="只处理此日期之前的视频，格式 2026-01-01")
    args = parser.parse_args()

    before = datetime.strptime(args.before, "%Y-%m-%d") if args.before else None
    results = BilibiliPipeline(frame_interval=args.interval).run(
        uid=args.uid, max_videos=args.videos, before_date=before
    )
    print(f"\n处理完成，共 {len(results)} 个视频:")
    for r in results:
        print(f"  - {r['title'][:40]} | 价格 {len(r['prices'])} 条 | "
              f"{'失败: ' + r['error'] if r.get('error') else '成功'}")
