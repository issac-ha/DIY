"""
B站爬虫：获取硬件UP主的视频内容（标题/简介/字幕/评论），供LLM提取价格

数据源优先级：
1. 视频字幕（CC字幕，信息最全）
2. 置顶评论（UP主常贴价格表）
3. 视频简介

注意：
- 使用 Cookie（从 .env 读取），不硬编码
- 请求限速，避免触发风控
- 只获取公开内容，不涉及隐私
"""
import hashlib
import time
from datetime import datetime
from urllib.parse import urlencode, quote

import requests

from app.config import BILIBILI_COOKIE
from app.core.logger import logger

# B站 wbi 签名的混淆表（固定值，B站公开算法）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com",
}

# B站对同一 IP 的接口调用有频控，连续请求会直接返回 code=-352（风控校验失败）。
# 每次请求至少间隔 2 秒；命中风控后按 5/15/30 秒退避重试，仍失败才放弃。
REQUEST_INTERVAL = 2.0
RISK_BACKOFF = (5, 15, 30)


class LoginRequiredError(RuntimeError):
    """Cookie 缺失或失效，采集必须终止

    B 站对匿名请求的风控阈值很低，没有有效登录态时接口会持续返回 -352，
    逐条退避重试既拿不到数据又白耗时间。所以统一用这个异常向上抛出，
    由入口提示用户重新扫码，不允许静默降级成匿名请求。
    """


class BilibiliCrawler:
    """B站视频内容爬虫"""

    def __init__(self):
        # 没有 Cookie 时直接失败，不构造匿名会话。
        # 放在构造阶段是为了让所有入口（CLI、API、脚本）都在启动时暴露问题，
        # 而不是跑到一半才发现每个请求都被风控。
        if not BILIBILI_COOKIE:
            raise LoginRequiredError(
                "未配置 BILIBILI_COOKIE，匿名请求会被风控拦截。"
                "请先执行 python -m app.crawler.bilibili_login 扫码登录"
            )
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.session.headers["Cookie"] = BILIBILI_COOKIE
        self._mixin_key = None
        self._last_request_at = 0.0

    # ---------- 请求限速 ----------

    def _throttle(self):
        """保证两次请求之间至少间隔 REQUEST_INTERVAL 秒"""
        wait = REQUEST_INTERVAL - (time.time() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.time()

    def _get(self, url: str, **kwargs):
        """
        限速 + 风控退避的 GET

        命中 code=-352 时按 RISK_BACKOFF 依次等待重试，调用方只需要看返回内容。
        非 JSON 响应（sprite 图片、字幕文件）直接返回，不参与风控判断。
        """
        timeout = kwargs.pop("timeout", 15)
        resp = None
        for delay in (0,) + RISK_BACKOFF:
            if delay:
                logger.warning(f"[bilibili] 触发风控(code=-352)，等待 {delay}s 后重试")
                time.sleep(delay)
            self._throttle()
            resp = self.session.get(url, timeout=timeout, **kwargs)
            try:
                code = resp.json().get("code")
            except ValueError:
                return resp
            if code != -352:
                return resp
        logger.error("[bilibili] 多次退避后仍被风控，放弃本次请求")
        return resp

    # ---------- 登录态 ----------

    def check_login(self) -> None:
        """
        校验登录态，无效时直接抛异常

        Cookie 过期后继续跑只会在每个接口上收到 -352，退避重试也拿不到数据。
        这里不返回布尔值，而是直接终止，避免调用方忘记处理返回值。
        """
        try:
            data = self._get("https://api.bilibili.com/x/web-interface/nav", timeout=10).json()
        except Exception as e:
            raise LoginRequiredError(f"登录状态检查失败，无法确认 Cookie 是否有效: {e}")
        if data.get("code") == 0 and (data.get("data") or {}).get("isLogin"):
            return
        raise LoginRequiredError(
            "Cookie 已失效（nav 返回 isLogin=False）。"
            "请执行 python -m app.crawler.bilibili_login 重新扫码登录"
        )

    # ---------- wbi 签名 ----------

    def _get_mixin_key(self) -> str:
        """获取 wbi 签名的 mixin_key（缓存）"""
        if self._mixin_key:
            return self._mixin_key
        try:
            resp = self._get("https://api.bilibili.com/x/web-interface/nav", timeout=10)
            data = resp.json()
            img_url = data["data"]["wbi_img"]["img_url"]
            sub_url = data["data"]["wbi_img"]["sub_url"]
            img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
            sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
            raw = img_key + sub_key
            self._mixin_key = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
        except Exception as e:
            logger.error(f"[bilibili] 获取 wbi key 失败: {e}")
            self._mixin_key = ""
        return self._mixin_key

    def _enc_wbi(self, params: dict) -> dict:
        """对参数进行 wbi 签名"""
        mixin_key = self._get_mixin_key()
        if not mixin_key:
            return params
        params["wts"] = int(time.time())
        params = dict(sorted(params.items()))
        query = urlencode(params)
        query = query.replace("!", "%21").replace("'", "%27").replace("(", "%28")
        query = query.replace(")", "%29").replace("*", "%2A")
        params["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
        return params

    # ---------- 视频列表 ----------

    def get_user_videos(self, uid: str, max_count: int = 30,
                        before_date: datetime = None) -> list:
        """
        获取UP主的视频列表（按发布时间倒序）

        :param uid: UP主UID
        :param max_count: 最多获取多少个视频
        :param before_date: 只获取此日期之前的视频（不含）
        :return: [{bvid, title, pubdate, aid, cid, ...}, ...]
        """
        videos = []
        page = 1
        page_size = 30
        self.check_login()

        while len(videos) < max_count:
            params = self._enc_wbi({
                "mid": uid,
                "ps": page_size,
                "pn": page,
                "order": "pubdate",
                "platform": "web",
                "web_location": "1550101",
            })
            try:
                resp = self._get(
                    "https://api.bilibili.com/x/space/wbi/arc/search",
                    params=params, timeout=15,
                )
                data = resp.json()
            except Exception as e:
                logger.error(f"[bilibili] 获取视频列表失败(uid={uid}, page={page}): {e}")
                break

            if data.get("code") != 0:
                logger.warning(f"[bilibili] 接口返回错误: code={data.get('code')}, "
                               f"msg={data.get('message')}")
                break

            vlist = data.get("data", {}).get("list", {}).get("vlist", [])
            if not vlist:
                break

            for v in vlist:
                pubdate = datetime.fromtimestamp(v.get("created", 0))
                # 日期过滤：只取 before_date 之前的视频
                if before_date and pubdate >= before_date:
                    continue
                videos.append({
                    "bvid": v.get("bvid", ""),
                    "title": v.get("title", ""),
                    "pubdate": pubdate,
                    "aid": v.get("aid", 0),
                    "cid": v.get("cid", 0),
                    "play": v.get("play", 0),
                    "length": v.get("length", ""),
                })
                if len(videos) >= max_count:
                    break

            page += 1

        logger.info(f"[bilibili] 获取到 {len(videos)} 个视频(uid={uid})")
        return videos

    # ---------- 视频内容 ----------

    def get_video_detail(self, bvid: str) -> dict:
        """获取视频详情（标题、简介、cid、aid）"""
        try:
            resp = self._get(
                f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                timeout=10,
            )
            data = resp.json()
            if data.get("code") == 0:
                v = data["data"]
                return {
                    "bvid": bvid,
                    "title": v.get("title", ""),
                    "desc": v.get("desc", ""),
                    "owner": v.get("owner", {}).get("name", ""),
                    "aid": v.get("aid"),
                    "cid": v.get("cid"),
                    "pubdate": datetime.fromtimestamp(v.get("pubdate", 0)),
                }
        except Exception as e:
            logger.error(f"[bilibili] 获取视频详情失败({bvid}): {e}")
        return {}

    def get_audio_urls(self, bvid: str, cid: int) -> list:
        """
        获取音频流候选地址（DASH audio，码率最低的一路，省流量）

        每条流都带 baseUrl 和 backupUrl 两组地址。baseUrl 常指向 PCDN 节点
        （如 edge.mountaintoys.cn:4483），部分网络连不上；backupUrl 一般是
        upos 官方源。只取第一条会在 PCDN 不可达时整期转写失败，所以全部返回，
        由调用方逐个尝试。

        :return: 候选地址列表，失败返回空列表
        """
        try:
            resp = self._get(
                "https://api.bilibili.com/x/player/playurl",
                params={"bvid": bvid, "cid": cid, "fnval": 4048, "fourk": 1},
                timeout=15,
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.error(f"[bilibili] 音频地址接口错误: {data.get('message')}")
                return []
            audios = (data.get("data", {}).get("dash") or {}).get("audio") or []
            if not audios:
                logger.warning(f"[bilibili] 无 DASH 音频流({bvid})")
                return []
            # 选码率最低的，语音转写够用
            audios.sort(key=lambda a: a.get("bandwidth", 0))
            audio = audios[0]
            urls = [audio.get("baseUrl") or audio.get("base_url")]
            urls += audio.get("backupUrl") or audio.get("backup_url") or []
            return [u for u in urls if u]
        except Exception as e:
            logger.error(f"[bilibili] 获取音频地址失败({bvid}): {e}")
            return []

    def get_subtitles(self, aid: int, cid: int) -> str:
        """获取视频字幕文本（CC字幕），无字幕返回空字符串"""
        if not aid or not cid:
            return ""
        try:
            resp = self._get(
                f"https://api.bilibili.com/x/player/v2?cid={cid}&aid={aid}",
                timeout=10,
            )
            data = resp.json()
            subtitles = data.get("data", {}).get("subtitle", {}).get("subtitles", [])
            if not subtitles:
                return ""
            sub_url = subtitles[0].get("subtitle_url", "")
            if not sub_url:
                return ""
            if not sub_url.startswith("http"):
                sub_url = "https:" + sub_url
            resp2 = self._get(sub_url, timeout=10)
            sub_data = resp2.json()
            lines = [item["content"] for item in sub_data.get("body", [])]
            return " ".join(lines)
        except Exception as e:
            logger.error(f"[bilibili] 获取字幕失败(aid={aid}): {e}")
            return ""

    def get_comments(self, aid: int, count: int = 10) -> list:
        """获取视频评论（按热度排序，置顶评论通常在最前面）"""
        if not aid:
            return []
        try:
            resp = self._get(
                f"https://api.bilibili.com/x/v2/reply?type=1&oid={aid}&pn=1&ps={count}&sort=1",
                timeout=10,
            )
            data = resp.json()
            if data.get("code") != 0:
                return []
            replies = data.get("data", {}).get("replies", []) or []
            comments = []
            for r in replies[:count]:
                comments.append({
                    "user": r.get("member", {}).get("uname", ""),
                    "content": r.get("content", {}).get("message", ""),
                    "likes": r.get("like", 0),
                })
            return comments
        except Exception as e:
            logger.error(f"[bilibili] 获取评论失败(aid={aid}): {e}")
            return []

    def get_video_snapshots(self, bvid: str, cid: int, output_dir: str = None,
                            interval: int = 0) -> list:
        """
        获取视频快照（B站官方的视频截帧，约每5-6秒一帧）

        原理：videoshot 接口返回一张 sprite 大图（如 10x10 网格），
        需要按网格坐标裁剪成单帧。

        :param bvid: 视频 BV 号
        :param cid: 视频 cid
        :param output_dir: 截图保存目录，不传则不保存到磁盘
        :param interval: 最小时间间隔(秒)，0 表示取所有帧
        :return: [{"time": 秒数, "path": 文件路径, "bytes": 图片字节}, ...]
        """
        import io
        from pathlib import Path
        from PIL import Image

        # videoshot 接口首次调用可能返回空 index（需触发后台生成），重试
        data = None
        for attempt in range(3):
            try:
                resp = self._get(
                    "https://api.bilibili.com/x/player/videoshot",
                    params={"index": 1, "cid": cid, "bvid": bvid},
                    timeout=15,
                )
                data = resp.json().get("data") or {}
            except Exception as e:
                logger.error(f"[bilibili] 获取快照接口失败({bvid}): {e}")
                return []
            if data.get("index"):
                break
            time.sleep(2)

        if not data or not data.get("index"):
            logger.warning(f"[bilibili] 视频无快照({bvid})")
            return []

        x_len = data["img_x_len"]
        y_len = data["img_y_len"]
        w = data["img_x_size"]
        h = data["img_y_size"]
        images = data.get("image", [])
        index = data.get("index", [])
        if not images:
            return []

        # 下载 sprite 大图
        sprite_url = images[0]
        if sprite_url.startswith("//"):
            sprite_url = "https:" + sprite_url
        try:
            sprite_bytes = self._get(sprite_url, timeout=20).content
            sprite = Image.open(io.BytesIO(sprite_bytes))
        except Exception as e:
            logger.error(f"[bilibili] 下载 sprite 失败({bvid}): {e}")
            return []

        # 准备输出目录
        if output_dir:
            Path(output_dir).mkdir(parents=True, exist_ok=True)

        snapshots = []
        last_time = -999
        for i, sec in enumerate(index):
            # 时间间隔过滤（用户要求每 N 秒一帧）
            if interval > 0 and sec - last_time < interval:
                continue
            last_time = sec

            col = i % x_len
            row = i // x_len
            box = (col * w, row * h, (col + 1) * w, (row + 1) * h)
            frame = sprite.crop(box)

            buf = io.BytesIO()
            frame.save(buf, format="JPEG", quality=92)
            frame_bytes = buf.getvalue()

            path = None
            if output_dir:
                path = str(Path(output_dir) / f"{bvid}_{sec:04d}s.jpg")
                frame.save(path, format="JPEG", quality=92)

            snapshots.append({"time": sec, "path": path, "bytes": frame_bytes})

        logger.info(f"[bilibili] 提取 {len(snapshots)} 张快照({bvid}, 视频{index[-1]}秒)")
        return snapshots

    def get_video_frames(self, bvid: str, cid: int, interval: int = 3,
                         output_dir: str = None, quality: int = 64,
                         max_seconds: int = None) -> list:
        """
        用 ffmpeg 从视频流按固定间隔抽帧，再由调用方做内容去重

        3 秒一帧的取舍：价格表通常停留十几秒以上，3 秒能稳定覆盖；
        同一张表的重复帧交给 dedupe_frames 合并，不会重复送给视觉模型。

        和 get_video_snapshots 的区别：
        - videoshot 是B站服务端生成的稀疏缩略图（5-6秒一张，480x270）
        - 本方法直接读视频流，间隔可精确控制，画质为原视频分辨率

        :param interval: 抽帧间隔(秒)，默认 3 秒
        :param quality: 清晰度 qn，16=360P 32=480P 64=720P 80=1080P
        :param max_seconds: 只处理前 N 秒（None 表示全片）
        :return: [{"time": 秒数, "path": 路径, "bytes": 图片字节}, ...]
        """
        import subprocess
        from pathlib import Path

        try:
            import imageio_ffmpeg
        except ImportError:
            logger.error("[bilibili] 未安装 imageio-ffmpeg，无法抽帧")
            return []

        # 1. 获取播放地址（DASH 格式）
        try:
            resp = self._get(
                "https://api.bilibili.com/x/player/playurl",
                params={
                    "bvid": bvid, "cid": cid, "qn": quality,
                    "fnval": 4048, "fourk": 1,
                },
                timeout=15,
            )
            data = resp.json()
        except Exception as e:
            logger.error(f"[bilibili] 获取播放地址失败({bvid}): {e}")
            return []

        if data.get("code") != 0:
            logger.error(f"[bilibili] 播放地址接口错误: {data.get('message')}")
            return []

        payload = data.get("data", {})
        dash_videos = (payload.get("dash") or {}).get("video") or []
        candidates = []
        if dash_videos:
            # 选最接近目标清晰度的流
            dash_videos.sort(key=lambda v: abs(v.get("id", 0) - quality))
            stream = dash_videos[0]
            candidates.append(stream.get("baseUrl") or stream.get("base_url"))
            candidates += stream.get("backupUrl") or stream.get("backup_url") or []
        else:
            durl = payload.get("durl") or []
            if durl:
                candidates.append(durl[0].get("url"))
                candidates += durl[0].get("backup_url") or []

        # baseUrl 常指向 PCDN 节点，部分网络连不上；backupUrl 是官方源。
        # 只试第一条会在 PCDN 不可达时整期抽帧失败，所以按顺序逐个尝试
        candidates = [u for u in candidates if u]
        if not candidates:
            logger.error(f"[bilibili] 未取得视频流地址({bvid})")
            return []

        # 2. ffmpeg 抽帧
        out_dir = Path(output_dir) if output_dir else Path("snapshots") / bvid
        out_dir.mkdir(parents=True, exist_ok=True)
        # 清掉旧帧，避免编号错乱
        for old in out_dir.glob("frame_*.jpg"):
            old.unlink()

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        headers = (
            "Referer: https://www.bilibili.com/\r\n"
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36\r\n"
        )
        cmd_tail = []
        if max_seconds:
            cmd_tail += ["-t", str(max_seconds)]
        cmd_tail += ["-vf", f"fps=1/{interval}", "-q:v", "3",
                     str(out_dir / "frame_%04d.jpg")]

        try:
            # 逐个候选地址尝试，第一条通了就不再试后面的
            proc = None
            for index, url in enumerate(candidates, start=1):
                # 上一条地址可能写出半套帧，换地址前清掉，避免帧序号和时间对不上
                if index > 1:
                    for old in out_dir.glob("frame_*.jpg"):
                        old.unlink()
                cmd = [
                    ffmpeg, "-y", "-loglevel", "error",
                    # B站 CDN 偶发 TLS 断连，让 ffmpeg 自己断点重连，比整段重来便宜得多
                    "-reconnect", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5",
                    "-headers", headers,
                    "-i", url,
                ] + cmd_tail
                # 卡住时最多等 5 分钟。原来 15 分钟在网络差时会长时间静默挂起，
                # 外层连重试的机会都没有
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if proc.returncode == 0:
                    break
                logger.error(
                    f"[bilibili] ffmpeg 抽帧失败(第 {index}/{len(candidates)} 条地址): "
                    f"{proc.stderr[:300]}"
                )
            if proc is None or proc.returncode != 0:
                return []
        except subprocess.TimeoutExpired:
            logger.error(f"[bilibili] ffmpeg 抽帧超时({bvid})")
            return []
        except Exception as e:
            logger.error(f"[bilibili] ffmpeg 调用异常: {e}")
            return []

        frames = []
        files = sorted(out_dir.glob("frame_*.jpg"))
        for idx, f in enumerate(files):
            frames.append({
                "time": idx * interval,
                "path": str(f),
                "bytes": f.read_bytes(),
            })

        logger.info(f"[bilibili] ffmpeg 抽帧完成: {len(frames)} 张, "
                    f"间隔 {interval} 秒({bvid})")
        return frames

    def get_video_content(self, bvid: str) -> dict:
        """
        获取视频的全部文本内容（标题+简介+字幕+评论），供LLM提取价格
        """
        detail = self.get_video_detail(bvid)
        if not detail:
            return {}
        subtitles = self.get_subtitles(detail.get("aid"), detail.get("cid"))
        comments = self.get_comments(detail.get("aid"), count=5)

        # 拼接所有文本
        text_parts = [f"标题: {detail.get('title', '')}"]
        if detail.get("desc"):
            text_parts.append(f"简介: {detail['desc']}")
        if subtitles:
            text_parts.append(f"字幕: {subtitles}")
        if comments:
            comment_text = "\n".join(
                f"{c['user']}: {c['content']}" for c in comments
            )
            text_parts.append(f"评论:\n{comment_text}")

        return {
            "bvid": bvid,
            "title": detail.get("title", ""),
            "owner": detail.get("owner", ""),
            "pubdate": detail.get("pubdate"),
            "full_text": "\n\n".join(text_parts),
        }


def dedupe_frames(frames: list, threshold: int = 3) -> list:
    """
    按感知哈希(dHash)去重，只保留画面有明显变化的帧

    同一个画面停留多秒会产生重复帧，这里把几乎完全相同的帧合并成一张。

    阈值必须保守（默认 3）：8x8 哈希只有 64 位，满屏密集表格在低分辨率下
    结构高度趋同。实测 CPU 表和闲鱼表相邻两帧距离只有 4，阈值设 8 会把
    闲鱼表整张丢掉。宁可多送几张给视觉模型，也不能漏表。

    :param frames: [{"time", "bytes", ...}, ...]
    :param threshold: 汉明距离阈值，<= 该值视为相同
    :return: 去重后的帧列表
    """
    import io

    from PIL import Image

    def dhash(data: bytes, size: int = 8) -> list:
        img = Image.open(io.BytesIO(data)).convert("L").resize((size + 1, size))
        px = list(img.getdata())
        bits = []
        for r in range(size):
            for c in range(size):
                bits.append(px[r * (size + 1) + c] > px[r * (size + 1) + c + 1])
        return bits

    kept = []
    prev_hash = None
    for f in frames:
        try:
            h = dhash(f["bytes"])
        except Exception:
            kept.append(f)
            continue
        if prev_hash is None or sum(a != b for a, b in zip(h, prev_hash)) > threshold:
            kept.append(f)
            prev_hash = h

    logger.info(f"[bilibili] 帧去重: {len(frames)} -> {len(kept)} 张")
    return kept


# 硬件价格类UP主列表（可扩展）
HARDWARE_UPLOADERS = {
    "芝士华莱士": "22697887",
}
