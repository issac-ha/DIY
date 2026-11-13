"""统一日志配置"""
import sys
from loguru import logger

# 移除默认输出，重新配置格式
logger.remove()
logger.add(
    sys.stdout,
    format="{time:HH:mm:ss} | {level: <7} | {message}",
    level="INFO",
)
logger.add(
    "logs/app.log",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {file}:{line} | {message}",
    level="DEBUG",
    rotation="10 MB",
    retention="7 days",
    encoding="utf-8",
)

__all__ = ["logger"]
