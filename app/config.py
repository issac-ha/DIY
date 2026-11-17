"""配置管理：从环境变量读取"""
import os
from dotenv import load_dotenv

load_dotenv()

# LLM 配置
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")

# 数据库
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./diy_agent.db")

# B站 Cookie（用于访问需要登录态的接口，如空间视频列表）
BILIBILI_COOKIE = os.getenv("BILIBILI_COOKIE", "")

# 视觉模型配置（火山方舟 Doubao-Seed-2.1-lite，支持文本+视觉+音频）
VISION_BASE_URL = os.getenv("VISION_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
VISION_MODEL = os.getenv("VISION_MODEL", "doubao-seed-2-1-lite-260915")
VISION_API_KEY = os.getenv("VISION_API_KEY", "")
