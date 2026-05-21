# edge-agent image — slim runtime that can also import backend.acquisition.
#
# M2: we now install the center's runtime deps so the edge-agent can run
# AcquisitionService in-process. The Django source itself comes from the
# bind-mounted ``backend/`` at runtime (see docker-compose.edge.yml).
#
# IMPORTANT: build with the repo root as build context, e.g.
#   docker build -f edge-agent/Dockerfile -t edge-agent .
# The compose file already passes ``context: .``.
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# modbus-tk / pyserial / paho-mqtt are pure-python; only ca-certificates
# is needed at the system layer. Optional binary stacks (snap7 for S7)
# are not required for the Modbus smoke path and are intentionally
# skipped to keep the image small and the build robust.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install backend requirements first so they live in a stable image layer.
COPY backend/requirements.txt /tmp/backend-requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/backend-requirements.txt

# Install the edge-agent package itself (editable so a bind-mount works).
COPY edge-agent/pyproject.toml edge-agent/README.md /app/
COPY edge-agent/src/ /app/src/
RUN pip install --no-cache-dir -e .

# Default env is empty; compose / k8s injects EDGE_ID / EDGE_TOKEN / CENTER_URL.
ENV LOG_LEVEL=INFO \
    DJANGO_SETTINGS_MODULE=control_plane.settings \
    BACKEND_DIR=/opt/backend \
    EDGE_STATE_DB=/var/lib/edge-agent/edge_state.db \
    PYTHONPATH=/opt/backend:/app/src

RUN mkdir -p /var/lib/edge-agent

CMD ["edge-agent"]
