"""Redis 缓存封装：给只读接口的结果缓存

三个设计约束：
1. 缓存不可用时静默降级，返回 None，调用方照常查库——缓存挂了不能连带接口挂掉
2. 值统一序列化成 JSON，排查时可以直接 redis-cli get 看内容
3. 所有异常在这里吞掉，不往外抛，缓存永远不是主流程的阻塞点
"""
import json
import time

from app.config import REDIS_URL
from app.core.logger import logger

# 连接失败后的冷却时间（秒）。Redis 没起来时每个请求都等 TCP 超时会拖慢整站，
# 失败一次就先停 30 秒不再尝试，期间全部走数据库
_RETRY_INTERVAL = 30
_retry_at = 0.0
_client = None
# 没配 REDIS_URL 就直接禁用，避免无谓的异常和日志
_disabled = not REDIS_URL


def _get_client():
    """懒加载连接；连不上就返回 None，由调用方降级查库"""
    global _client, _retry_at
    if _disabled:
        return None
    if _client is not None:
        return _client
    now = time.time()
    if now < _retry_at:
        return None
    try:
        import redis

        _client = redis.from_url(
            REDIS_URL,
            # 直接返回 str，省去每次手动 decode
            decode_responses=True,
            # 超时要短：缓存是加速手段，不能为了等它把接口拖慢
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        _client.ping()
        logger.info(f"[cache] Redis 已连接: {REDIS_URL}")
        return _client
    except Exception as e:
        _client = None
        _retry_at = now + _RETRY_INTERVAL
        logger.warning(
            f"[cache] Redis 不可用，{_RETRY_INTERVAL}s 内降级为直查数据库: {e}"
        )
        return None


def cache_get(key: str):
    """读缓存。未命中、未启用、出错都返回 None"""
    client = _get_client()
    if client is None:
        return None
    try:
        raw = client.get(key)
    except Exception as e:
        logger.warning(f"[cache] 读取失败 {key}: {e}")
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 值被写坏（比如手工改过）时按未命中处理，下次会重新写对的进去
        logger.warning(f"[cache] 缓存值不是合法 JSON，按未命中处理: {key}")
        return None


def cache_set(key: str, value, ttl: int = 300) -> None:
    """写缓存，默认 5 分钟过期。写失败只记日志，不影响接口返回"""
    client = _get_client()
    if client is None:
        return
    try:
        # default=str 让 date/datetime 这类对象能序列化，不用每个调用点手动转
        client.set(key, json.dumps(value, ensure_ascii=False, default=str), ex=ttl)
    except Exception as e:
        logger.warning(f"[cache] 写入失败 {key}: {e}")


def cache_delete_prefix(prefix: str) -> int:
    """按前缀批量删除，采集完成后清理受影响的数据

    用 scan_iter 而不是 keys：keys 是全库扫描，数据量大时会阻塞 Redis
    的单线程主循环，生产环境属于禁用命令。
    """
    client = _get_client()
    if client is None:
        return 0
    try:
        keys = list(client.scan_iter(match=f"{prefix}*", count=500))
        if keys:
            client.delete(*keys)
        return len(keys)
    except Exception as e:
        logger.warning(f"[cache] 清理失败 {prefix}: {e}")
        return 0
