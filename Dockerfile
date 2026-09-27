FROM python:3.12-slim

# 不写 .pyc、日志直出
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CRACK_DB_PATH=/data/crack_review.db

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app ./app

# SQLite 数据卷
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 容器启动时自动建表（FastAPI startup 钩子）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
