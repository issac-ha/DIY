"""
视频音频转写：ffmpeg 提取音频 -> faster-whisper 转成文字

用途：B站很多视频没有 CC 字幕，但价格表视频通常有旁白，
把音频转成文字后可以和截图识别结果一起喂给 LLM。

流程：
1. 从 DASH 音频流拉取音频（不下载整个视频，省流量）
2. ffmpeg 转成 16k 单声道 wav（whisper 要求）
3. faster-whisper 中文转写（CPU int8 量化，无需 GPU）
"""
import subprocess
from pathlib import Path

import imageio_ffmpeg

from app.core.logger import logger

# 模型大小：tiny/base/small/medium/large-v3
# 中文硬件术语建议 small 以上，small 在 CPU 上约 4-6 倍实时速度
MODEL_SIZE = "small"

# 首次加载后复用，避免每次重新加载模型（加载本身要几秒）
_model = None

_HEADERS = (
    "Referer: https://www.bilibili.com/\r\n"
    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36\r\n"
)

# 领域提示词，提升显卡相关术语的识别准确率
_INITIAL_PROMPT = "以下是显卡价格行情视频的解说，常见词：显卡、RTX、RX、显存、价格、涨跌。"


def _get_model():
    """懒加载 whisper 模型"""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        logger.info(f"[asr] 加载 whisper 模型 {MODEL_SIZE}（首次运行需下载）")
        _model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
        logger.info("[asr] 模型加载完成")
    return _model


def download_audio(audio_url: str, out_path: Path) -> bool:
    """
    用 ffmpeg 把音频流转成 16k 单声道 wav

    :return: 是否成功
    """
    if not audio_url:
        return False

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-headers", _HEADERS,
        "-i", audio_url,
        "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
        str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if proc.returncode != 0:
            logger.error(f"[asr] 音频提取失败: {proc.stderr[:200]}")
            return False
        if not out_path.exists() or out_path.stat().st_size < 1024:
            logger.error("[asr] 音频文件为空")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error("[asr] 音频提取超时")
        return False
    except Exception as e:
        logger.error(f"[asr] 音频提取异常: {e}")
        return False


def transcribe(audio_path: Path) -> str:
    """
    语音转文字

    :return: 纯文本（各段已拼接）
    """
    model = _get_model()
    try:
        segments, info = model.transcribe(
            str(audio_path),
            language="zh",
            beam_size=5,
            vad_filter=True,
            initial_prompt=_INITIAL_PROMPT,
        )
        parts = []
        for seg in segments:
            text = seg.text.strip()
            if text:
                parts.append(text)
        result = "".join(parts)
        logger.info(f"[asr] 转写完成: {len(result)} 字, 音频 {info.duration:.0f} 秒")
        return result
    except Exception as e:
        logger.error(f"[asr] 转写失败: {e}")
        return ""


def transcribe_video(crawler, bvid: str, cid: int, audio_dir: Path) -> str:
    """
    一步完成：取音频地址 -> 下载 -> 转写

    :param crawler: BilibiliCrawler 实例
    :param audio_dir: 音频缓存目录
    :return: 转写文本，失败返回空字符串
    """
    audio_url = crawler.get_audio_url(bvid, cid)
    if not audio_url:
        return ""

    audio_path = Path(audio_dir) / f"{bvid}.wav"
    if not download_audio(audio_url, audio_path):
        return ""

    return transcribe(audio_path)
