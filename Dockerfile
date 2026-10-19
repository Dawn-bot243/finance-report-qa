FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# curl 供 compose healthcheck 使用
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 代码与配置；数据（db/ raw/ reports/）由 compose 挂载
COPY run_all.py config.yaml ./
COPY src/ src/
COPY app/ app/
COPY data/ data/
COPY scripts/ scripts/

EXPOSE 8000 8501

# 默认启动后端；compose 中 frontend 服务覆盖 command
CMD ["uvicorn", "src.api.server:app", "--host", "0.0.0.0", "--port", "8000"]
