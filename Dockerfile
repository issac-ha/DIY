# 用 slim 版而不是完整版，镜像能小一半以上。
# Python 版本和本地开发保持一致，避免依赖在不同小版本上行为不同
FROM python:3.10-slim

# PYTHONUNBUFFERED：日志立刻输出，否则 docker logs 看不到实时进度
# PYTHONDONTWRITEBYTECODE：容器里不需要 .pyc，省一层磁盘写入
# PIP_NO_CACHE_DIR：不保留 pip 下载缓存，减小镜像体积
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ctranslate2 是 faster-whisper 的推理后端，依赖 libgomp 这个 OpenMP 运行库。
# slim 镜像默认没有，不装会在导入 faster_whisper 时直接报错
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先只拷依赖清单，只要 requirements.txt 没变，这一层就能复用缓存，
# 改业务代码时不需要重新下载全部依赖
COPY requirements.txt .
# 国内网络直连 PyPI 很慢，走清华源。部署到海外可以去掉 -i 参数
RUN pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 只拷应用代码。数据库文件、日志、采集产物都通过挂载卷提供，
# 不打进镜像，否则每次构建都会带着历史数据
COPY app ./app

# 采集和运行时会写这些目录，提前建好并保证容器内用户有写权限
RUN mkdir -p /app/audio /app/snapshots /app/reports /app/logs

EXPOSE 8000

# 监听 0.0.0.0 而不是 127.0.0.1，否则容器外访问不到
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
