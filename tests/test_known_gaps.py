"""已知缺口清单测试：验证重试计数、确认阈值和补齐后的清理"""
import json

import pytest

from app.crawler import bilibili_pipeline as bp


@pytest.fixture
def gaps_file(tmp_path, monkeypatch):
    """把缺口清单指向临时目录，避免测试写到真实的 app/data 下"""
    path = tmp_path / "known_gaps.json"
    monkeypatch.setattr(bp, "KNOWN_GAPS_FILE", path)
    return path


def test_record_attempt_increments_and_confirms(gaps_file):
    """连续失败达到阈值后标记为已确认"""
    assert bp.load_known_gaps() == {}

    for i in range(1, bp.MAX_GAP_ATTEMPTS + 1):
        item = bp.record_gap_attempt("2026-10-05", "整期无有效价格数据")
        assert item["attempts"] == i
        assert item["confirmed"] is (i >= bp.MAX_GAP_ATTEMPTS)

    saved = json.loads(gaps_file.read_text(encoding="utf-8"))
    assert saved["2026-10-05"]["reason"] == "整期无有效价格数据"


def test_clear_gap_removes_record(gaps_file):
    """补齐后清空记录，避免以后被误跳过"""
    bp.record_gap_attempt("2026-10-05", "显卡 0/80")
    bp.clear_gap("2026-10-05")
    assert bp.load_known_gaps() == {}


def test_clear_gap_ignores_missing_date(gaps_file):
    """清除不存在的日期不应报错"""
    bp.clear_gap("2026-01-01")
    assert bp.load_known_gaps() == {}


def test_corrupted_file_returns_empty(gaps_file):
    """文件损坏时按空处理，不能阻塞采集"""
    gaps_file.write_text("{not json", encoding="utf-8")
    assert bp.load_known_gaps() == {}
