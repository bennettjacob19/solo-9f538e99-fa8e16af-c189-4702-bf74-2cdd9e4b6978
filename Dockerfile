FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TAPE_ROOT=/data/tapes \
    OUTPUT_ROOT=/data/output

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py ./run.py
COPY scripts ./scripts

# 镜像内置样例带卷；compose 可用卷覆盖 /data/tapes。
RUN python -I scripts/make_samples.py --root /data/tapes

RUN mkdir -p /data/output

EXPOSE 8000

# 单 worker + 多线程：恢复任务的进程内状态与锁需要单进程语义。
CMD ["python", "-m", "gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", "600", "run:app"]
