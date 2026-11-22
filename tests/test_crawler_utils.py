"""爬虫工具函数测试：只测纯函数，不碰网络和模型"""
from app.crawler.audio_transcriber import segments_to_text


def test_segments_to_text_joins_text():
    """分段按原样拼接，不加分隔符"""
    segments = [
        {"start": 0.0, "end": 2.5, "text": "本期显卡"},
        {"start": 2.5, "end": 5.0, "text": "价格继续下跌"},
    ]
    assert segments_to_text(segments) == "本期显卡价格继续下跌"


def test_segments_to_text_empty_list():
    """空列表返回空串，调用方可以直接入库"""
    assert segments_to_text([]) == ""


def test_segments_to_text_none():
    """None 也要安全返回空串"""
    assert segments_to_text(None) == ""


def test_segments_to_text_missing_text_key():
    """个别分段没有 text 字段时跳过，不抛异常"""
    segments = [{"start": 0.0}, {"start": 1.0, "text": "正常"}]
    assert segments_to_text(segments) == "正常"
