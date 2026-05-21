# M2 smoke test — 配置下发 + 单 task 在 edge 上跑

End-to-end verification for the M2 milestone (XIU-59): register an edge,
build a Modbus task on the center, dispatch it to the edge, and confirm
the edge runs the acquisition pipeline locally and reports state back.

Prerequisites: Docker + Docker Compose. The mock Modbus device is bundled
in `docker-compose.edge.yml` — no real PLC hardware needed.

## 1. Start the center

```bash
docker compose -f docker-compose.center.yml up -d --build
# wait for django to be healthy
curl -s localhost:8000/api/fleet/edges/ | head
```

The center runs django + redis + frontend. It does NOT run acquisition
workers or InfluxDB — those live on the edge.

## 2. Register an edge (center side)

In the frontend `/fleet` page click "注册 edge", or via API:

```bash
curl -s -X POST localhost:8000/api/fleet/edges/ \
  -H 'Content-Type: application/json' \
  -d '{"name": "edge-smoke-1", "labels": {"site": "lab"}}'
```

The response contains a one-shot `activation_token`. Copy it.

## 3. Start the edge stack

The edge compose builds an image that bundles the center's runtime deps so
the edge-agent can `import backend.acquisition` and run the pipeline
in-process. Build context is the repo root.

```bash
export EDGE_ID=edge-smoke-1
export EDGE_TOKEN=<activation_token from step 2>
# CENTER_URL points at the center's /ws/fleet/ endpoint. Use the host's
# LAN IP (not localhost) so the edge container can reach it.
export CENTER_URL=ws://<center-host-ip>:8000/ws/fleet/

docker compose -f docker-compose.edge.yml up -d --build
docker compose -f docker-compose.edge.yml logs -f edge-agent
```

Within a few seconds the edge-agent log shows
`registered with center as edge=edge-smoke-1` and the `/fleet` page flips
the edge to `online`.

## 4. Build a Modbus task pointed at the edge's mock device

On the center, create a device + points + task. The device's
`ip_address` must be `mock-modbus-edge` (the mock service's container name
on the edge network) port `5020`.

Via the frontend: `/connections` → 新建 Modbus TCP 设备 (`mock-modbus-edge:5020`)
→ add 9 holding-register points → `/acquisition` 新建 AcqTask 关联这些测点.

Then assign the task to the edge:

```bash
# edge_id is the EdgeNode.id from step 2's response
curl -s -X PATCH localhost:8000/api/config/tasks/<task_id>/ \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": <edge_id>}'
```

## 5. Push the config to the edge

```bash
curl -s -X POST localhost:8000/api/fleet/edges/<edge_id>/assignments/sync/
```

This reconciles `EdgeAssignment` rows, builds a full `apply_config`
snapshot (tasks + devices + points), and pushes it over the edge's WS.
The response summary lists `config_version`, `assignment_count`,
`device_count`, `point_count`.

## 6. Verify acquisition is running

Within ~60 s:

- **Edge InfluxDB** has samples for the 9 registers:

  ```bash
  docker exec edge-influxdb influx query \
    'from(bucket:"iot-data") |> range(start:-2m) |> limit(n:5)' \
    --org edge-iot --token my-super-secret-auth-token
  ```

- **Center `/acquisition` page** shows the task as `running`. The state is
  served by `GET /api/fleet/task-statuses/?task=<task_id>`:

  ```bash
  curl -s 'localhost:8000/api/fleet/task-statuses/?task=<task_id>'
  # → [{"edge_name": "edge-smoke-1", "task_code": "...", "state": "running", ...}]
  ```

## 7. Verify auto-resume on restart

Restart the edge-agent container and confirm acquisition resumes from the
local SQLite cache without re-pushing config:

```bash
docker compose -f docker-compose.edge.yml restart edge-agent
docker compose -f docker-compose.edge.yml logs -f edge-agent
```

The log shows `replay: cached apply_config v=<n>` followed by the task
threads spinning back up. New samples land in the edge InfluxDB within
~30 s. This works because the `apply_config` snapshot is persisted in the
`edge_state.db` SQLite, which lives in the `edge-agent-state` named volume.

## Teardown

```bash
docker compose -f docker-compose.edge.yml down
docker compose -f docker-compose.center.yml down
```

## Notes / known limitations (M2 scope)

- The edge-agent runs the pipeline in-process (no Celery on the edge); one
  OS thread per task.
- `WebSocketSink` on the edge has no Redis to push to — operator-UI lifecycle
  events degrade silently. Sample writes to InfluxDB are unaffected.
- 1 Hz aggregated sample upload to the center is **M3**, not M2. M2 only
  proves the task *runs* on the edge and reports lifecycle state.
