# Flux 后端镜像（主规格 §18.3 Docker 部署）
#
# 用途：容器内运行 FastAPI 服务（uvicorn），并在启动前把数据库迁移推到最新版本。
# 基础镜像 python:3.10-slim 与本机开发环境（Python 3.10）保持一致；
# 依赖全部有预编译 wheel（asyncpg 亦提供 wheel），因此不安装任何 apt 包。

FROM python:3.10-slim

# 不写 .pyc 字节码、日志实时刷出、pip 不保留下载缓存
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先只拷贝依赖清单并安装，改动业务代码时可以命中这一层缓存
COPY requirements.txt ./
RUN pip install -r requirements.txt

# 再拷贝运行所需的迁移脚本与应用包（tests 等由 .dockerignore 排除）
COPY alembic.ini ./
COPY migrations ./migrations
COPY flux ./flux

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn flux.main:app --host 0.0.0.0 --port 8000"]
