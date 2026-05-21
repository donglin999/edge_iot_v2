# edge-agent image — keep slim; M2+ will layer acquisition deps on top.
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# We deliberately install editable so a bind-mounted `edge-agent/` in dev
# does not require a rebuild between iterations. Same Dockerfile is used
# in CI by passing `--target=...` (M2+).
COPY pyproject.toml README.md /app/
COPY src/ /app/src/

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e .

# Default env is empty; compose / k8s injects EDGE_ID / EDGE_TOKEN / CENTER_URL.
ENV LOG_LEVEL=INFO

CMD ["edge-agent"]
