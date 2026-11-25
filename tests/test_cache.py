"""缓存模块的降级测试

只验证「Redis 不可用时不影响主流程」这一条约束，不依赖真实 Redis 实例。
真正的读写行为已在本地手动验证过，单元测试覆盖降级路径即可。
"""
import app.core.cache as cache


def test_disabled_cache_is_noop(monkeypatch):
    """未连接 Redis 时：读返回 None，写不报错，按前缀清理返回 0"""
    monkeypatch.setattr(cache, "_disabled", True)

    assert cache.cache_get("prices:2026-11-20:gpu:new") is None
    cache.cache_set("prices:2026-11-20:gpu:new", {"a": 1}, ttl=60)
    assert cache.cache_delete_prefix("prices:") == 0


def test_invalidate_report_cache_when_disabled(monkeypatch):
    """采集结束后的清理在缓存不可用时返回 0，采集流程不该感知异常"""
    monkeypatch.setattr(cache, "_disabled", True)

    assert cache.invalidate_report_cache() == 0
