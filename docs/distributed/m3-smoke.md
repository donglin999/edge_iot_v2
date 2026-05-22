# M3 smoke test — lifecycle + sample_batch 上行 + spill 降级

End-to-end verification for the M3 milestone ([XIU-63] uplink). Extends the
M2 smoke runbook (`m2-smoke.md`): M2 proved a task *runs* on the edge; M3
proves the edge ships its lifecycle events and 1 Hz aggregated samples up
to the center, and that the InfluxDB spill queue degrades gracefully under
the edge's read-only `backend/` mount.

Protocol contract: `protocol.md` v0.3 (`lifecycle` + `sample_batch`
frames, per-edge `monotonic_seq`).

Prerequisites: Docker + Docker Compose. Mock Modbus device is bundled in
`docker-compose.edge.yml` — no real PLC needed.

## 1. Start the center

```bash
docker compose -f docker-compose.center.yml up -d --build
# wait for django
until curl -sf localhost:8000/api/fleet/edges/ >/dev/null; do sleep 2; done
```

## 2. Register an edge

```bash
curl -s -X POST localhost:8000/api/fleet/edges/ \
  -H 'Content-Type: application/json' \
  -d '{"name": "edge-smoke-m3", "labels": {"site": "lab"}}'
# → note the EdgeNode "id" and the one-shot "activation_token"
```

## 3. Start the edge stack

`CENTER_URL` must use the host's LAN IP (not `localhost`) so the edge
container can reach the center.

```bash
export EDGE_ID=edge-smoke-m3
export EDGE_TOKEN=<activation_token from step 2>
export CENTER_URL=ws://<center-host-ip>:8000/ws/fleet/

docker compose -f docker-compose.edge.yml up -d --build
docker compose -f docker-compose.edge.yml logs -f edge-agent
```

Within a few seconds the log shows
`registered with center as edge=edge-smoke-m3 proto=0.3`. Note the
`proto=0.3` — a v0.3 edge emits `lifecycle`/`sample_batch`, not the v0.2
`task_state` frame.

## 4. Build + assign + push a Modbus task

Same as M2 smoke steps 4–5 (device `mock-modbus-edge:5020`, 9 holding
points, AcqTask, `PATCH .../tasks/<id>/ {edge_id}`, then
`POST /api/fleet/edges/<edge_id>/assignments/sync/`). The edge applies the
snapshot and the pipeline starts reading.

> If the `edge-agent-state` named volume already holds a cached
> `apply_config` from a prior run, the agent **replays it on boot**
> (`replay: cached apply_config v=<n>`) — that is M2 auto-resume, and the
> M3 uplink checks below work identically against a replayed config.

## 5. Verify `lifecycle` frames reached the center

```bash
curl -s 'localhost:8000/api/fleet/lifecycle-events/?edge=<edge_id>' | python3 -m json.tool
```

Expect (newest first), all carrying a strictly increasing `monotonic_seq`:

- `session.online`        — control plane up
- `task.starting`         — runner spawning the task thread
- `task.running`          — acquisition reading

`EdgeTaskStatus` folds the `task.*` events into the current-state row:

```bash
curl -s 'localhost:8000/api/fleet/task-statuses/?edge=<edge_id>'
# → [{"state": "running", "task_code": "...", ...}]
```

PASS criteria: ≥3 lifecycle rows; `EdgeTaskStatus.state == "running"`.

## 6. Verify `sample_batch` frames reached the center

```bash
curl -s 'localhost:8000/api/fleet/samples/?edge=<edge_id>' | python3 -m json.tool
```

Expect one `EdgeSample` row per point (9 rows for the smoke device). Run
the query twice ~3 s apart — `value`, `monotonic_seq` and `window_end`
must advance. The center logs each frame:

```
fleet: sample_batch edge=8 task=smoke-task seq=109 samples=9 cached=9
```

`EdgeNode.last_uplink_seq` must climb monotonically:

```bash
docker compose -f docker-compose.center.yml exec -T django python -c \
  "import django;django.setup();from fleet.models import EdgeNode;\
print(EdgeNode.objects.get(name='edge-smoke-m3').last_uplink_seq)"
```

PASS criteria: 9 `EdgeSample` rows, values updating, `last_uplink_seq`
increasing.

## 7. Verify the InfluxDB spill queue degrades gracefully

The edge mounts the center's `backend/` **read-only** (`/opt/backend:ro`),
so the legacy spill-DB location (`backend/influx_spill.sqlite3`) is
unwritable. `edge_agent.django_setup` redirects the spill DB to the
writable state volume via `INFLUXDB_SPILL_DB_PATH`.

```bash
# spill DB lands on the writable volume, NOT the read-only mount:
docker compose -f docker-compose.edge.yml exec -T edge-agent \
  sh -c 'ls -la /var/lib/edge-agent/influx_spill.sqlite3; \
         touch /opt/backend/__wtest 2>&1 || echo "/opt/backend read-only confirmed"'

# no ERROR about the spill queue (only the known M2 WebSocketSink/Redis
# WARNING spam, which is unrelated):
docker compose -f docker-compose.edge.yml logs edge-agent 2>&1 \
  | grep -iE 'spill' || echo "no spill log lines — spill queue opened cleanly"
```

PASS criteria: `influx_spill.sqlite3` exists under `/var/lib/edge-agent/`;
`/opt/backend` is read-only; **no `ERROR` line** mentioning the spill
queue. If even the writable path fails, the expected log is a single
`WARNING` ("durable spill disabled, continuing without it") — never an
`ERROR`, and live InfluxDB writes keep working regardless.

## 8. Verify uplink resume on agent restart

```bash
docker compose -f docker-compose.edge.yml restart edge-agent
sleep 12
```

`monotonic_seq` is *not* persisted across restarts (M3 scope) — the new
process stream restarts at 1. The center detects this and logs:

```
fleet: edge=edge-smoke-m3 uplink stream reset (agent restart) last=141 got=1
```

`last_uplink_seq` resets to the new low stream; a fresh
`session.online` / `task.starting` / `task.running` triple is appended to
`EdgeLifecycleEvent`; the WS close also synthesises a `session.offline`
row (`seq=0`, center-generated — a closing socket cannot send a final
frame). Samples resume within ~30 s.

PASS criteria: center logs the `uplink stream reset` line; new lifecycle
rows appear; samples resume.

## Teardown

```bash
docker compose -f docker-compose.edge.yml down       # add -v to wipe the
docker compose -f docker-compose.center.yml down     # cached apply_config
```

## Last verified

2026-05-22, branch `distributed/m3-uplink` ([XIU-65]) — all 8 steps
green. Full backend suite 382 passed / 1 skipped; edge-agent suite 37
passed.

[XIU-63]: ../../  "distributed M3 uplink"
[XIU-65]: ../../  "M3 docker smoke + unit regression"
