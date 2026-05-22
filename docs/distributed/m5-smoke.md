# M5 smoke test — 离线降级 + 断线回补 + outbox 持久化

End-to-end verification for the M5 milestone ([XIU-71]). Extends the M3/M4
smoke runbooks (`m3-smoke.md`, `m4-smoke.md`): M3 proved the online uplink
stream, M4 the alarm channel. M5 proves the edge survives the **center
being unreachable for ≥ 1 h** — including an **edge-agent restart mid
outage** — and on reconnect backfills every buffered frame with **zero
loss and zero duplicates**.

Protocol contract: `protocol.md` v0.5 — `register.uplink_seq`,
`ack.last_uplink_seq`, `heartbeat.buffer`, the `backfill` frame tag, and
the lit-up `alarm_event.status: "cleared"`.

Prerequisites: Docker + Docker Compose. Mock Modbus device is bundled in
`docker-compose.edge.yml` — no real PLC needed.

The hard acceptance bar (from the issue): **拔中心 ≥ 1 h + 离线期 edge 重启
一次,恢复后零丢失、零重复**. The `≥ 1 h` window is compressed by raising
the uplink rate (`EDGE_UPLINK_SAMPLE_WINDOW=0.1`) — 10 min of wall clock
at 10 Hz buffers the same frame count as 1 h at the spec'd 1 Hz, while
also exercising the durable-outbox depth.

## Option A — scripted (recommended)

`scripts/chaos_offline_backfill.py` automates steps 1–9 below: it brings
the stack up, registers an edge, pushes a Modbus task + an alarm rule,
stops the center container, restarts the edge-agent mid-outage, restores
the center, then asserts zero-loss / zero-duplicate on the center side.

```bash
python3 scripts/chaos_offline_backfill.py --offline-seconds 120 --restart-edge
```

Exit code `0` = PASS. The script prints a per-stream
(`lifecycle` / `sample_batch` / `alarm_event`) reconciliation table. Use
Option B below to run / inspect the steps by hand.

## 1. Start the center

```bash
docker compose -f docker-compose.center.yml up -d --build
until curl -sf localhost:8000/api/fleet/edges/ >/dev/null; do sleep 2; done
```

## 2. Register an edge

```bash
curl -s -X POST localhost:8000/api/fleet/edges/ \
  -H 'Content-Type: application/json' \
  -d '{"name": "edge-smoke-m5", "labels": {"site": "lab"}}'
# → note the EdgeNode "id" and the one-shot "activation_token"
```

## 3. Start the edge stack

```bash
export EDGE_ID=edge-smoke-m5
export EDGE_TOKEN=<activation_token from step 2>
export CENTER_URL=ws://<center-host-ip>:8000/ws/fleet/
export EDGE_UPLINK_SAMPLE_WINDOW=0.1   # 10 Hz — compresses the 1 h window

docker compose -f docker-compose.edge.yml up -d --build
docker compose -f docker-compose.edge.yml logs -f edge-agent
```

The log shows `registered with center as edge=edge-smoke-m5 proto=0.5
center_seq=0` and `durable uplink outbox ready (depth=0 high_seq=0)`.

## 4. Build + assign + push a Modbus task + alarm rule

Same as M4 smoke steps 4–5 (device `mock-modbus-edge:5020`, holding
points, AcqTask, `PATCH .../tasks/<id>/ {edge_id}`,
`POST /api/fleet/edges/<edge_id>/assignments/sync/`, then create one
threshold `AlarmRule` on a point the mock device crosses). Confirm the
edge is acquiring and shipping `sample_batch` frames:

```bash
curl -s 'localhost:8000/api/fleet/samples/?edge=<edge_id>' | python3 -m json.tool
```

Record the center's current high-water seq — it will be the backfill
baseline:

```bash
curl -s localhost:8000/api/fleet/edges/<edge_id>/ \
  | python3 -c 'import sys,json; e=json.load(sys.stdin); print("last_uplink_seq", e["last_uplink_seq"])'
```

## 5. Pull the center (拔中心)

```bash
docker compose -f docker-compose.center.yml stop center
```

The edge-agent log now shows `connect failed … retry in Ns` on the
exponential backoff. **Crucially it keeps acquiring**: `apply_config`
replayed from `edge_state.db`, the Modbus reads and local `AlarmSink`
evaluation continue, and every `lifecycle` / `sample_batch` /
`alarm_event` frame lands in the durable SQLite outbox. Heartbeat
`buffer` climbs (visible once the center is back).

Inspect the buffer growing while offline:

```bash
docker compose -f docker-compose.edge.yml exec -T edge-agent \
  sh -c 'sqlite3 /var/lib/edge-agent/edge_state.db \
   "SELECT COUNT(*), MAX(seq) FROM edge_uplink_outbox;"'
```

PASS criteria for this step: the count climbs steadily; the edge-agent
process stays up and does not block or crash.

## 6. Restart the edge-agent mid-outage (验证 outbox 持久化)

While the center is still down:

```bash
docker compose -f docker-compose.edge.yml restart edge-agent
```

After the restart the log shows `durable uplink outbox ready (depth=<N>
high_seq=<M>)` with **N > 0** — the buffered frames survived the restart.
Acquisition resumes and the seq counter continues from `M+1`, not `1`.

PASS criteria: `depth` and `high_seq` after the restart match (≈) the
pre-restart `COUNT(*)` / `MAX(seq)` — nothing was lost.

## 7. Restore the center

```bash
docker compose -f docker-compose.center.yml start center
until curl -sf localhost:8000/api/fleet/edges/ >/dev/null; do sleep 2; done
```

The edge-agent reconnects within one backoff cycle. Its log shows
`registered … center_seq=<baseline>` then `reconnect backfill — <N>
frame(s) pending`. The center log shows a burst of `lifecycle` /
`sample_batch` / `alarm_event` lines as the backfill drains (batched /
throttled — `EDGE_BACKFILL_BATCH` frames, then a `EDGE_BACKFILL_PAUSE_S`
pause).

## 8. Verify zero loss + zero duplicates

```bash
# The center high-water seq must equal the edge's outbox high_seq.
curl -s localhost:8000/api/fleet/edges/<edge_id>/ | python3 -m json.tool
```

- `last_uplink_seq` on the center == the edge outbox `high_seq` from
  step 6 → **zero loss** (every buffered frame was accepted).
- `buffer_backlog` returns to `0` once the backfill drains.
- No duplicate rows: the center's `classify_uplink_seq` drops any
  re-sent frame the center already had, so an `EdgeLifecycleEvent` /
  `EdgeSample` / `Alarm` count taken before vs. after the backfill shows
  **exactly one row per distinct event**, never doubles. Spot-check:

```bash
docker compose -f docker-compose.center.yml exec -T center \
  python manage.py shell -c \
  'from fleet.models import EdgeLifecycleEvent; \
   qs=EdgeLifecycleEvent.objects.values_list("monotonic_seq",flat=True); \
   s=list(qs); print("rows",len(s),"distinct",len(set(s)),"dups",len(s)-len(set(s)))'
```

`dups 0` is the PASS line. The edge outbox empties:

```bash
docker compose -f docker-compose.edge.yml exec -T edge-agent \
  sh -c 'sqlite3 /var/lib/edge-agent/edge_state.db \
   "SELECT COUNT(*) FROM edge_uplink_outbox;"'   # → 0 once acked
```

## 9. Verify the alarm clear-transition uplink

Raise the mock value above the threshold during the run (alarm fires),
then let it drop back in range while **offline**. After the backfill the
center `/alarms` page shows the alarm as `cleared`, not stuck `firing`:

```bash
curl -s 'localhost:8000/api/acquisition/alarms/?edge=<edge_id>' | python3 -m json.tool
```

PASS criteria: the alarm that fired offline reaches the center as a
`firing` row that is then closed by the backfilled `cleared` frame —
`status: "cleared"`, `cleared_at` set.

## Teardown

```bash
docker compose -f docker-compose.edge.yml down -v     # -v wipes edge_state.db
docker compose -f docker-compose.center.yml down
```

## Last verified

Unit suites green on branch `distributed/m5-offline` ([XIU-71]):
backend `tests/test_fleet_m5.py` + `test_fleet*` 68 passed,
`tests/` acquisition/alarm 100 passed (1 pre-existing Redis env-skip);
edge-agent suite 68 passed (incl. new `test_outbox.py` /
`test_offline_backfill.py`). The Docker chaos steps 1–9 above are the
QA回归 checklist — to be run by the test engineer before integration
review (same as M3 [XIU-65] / M4 [XIU-69]).

[XIU-71]: ../../  "distributed M5 offline + backfill"
