# M4 smoke test — alarm rule sync + alarm 上行 + 来源 edge 标注

End-to-end verification for the M4 milestone ([XIU-69] alarms). Extends the
M3 smoke runbook (`m3-smoke.md`): M3 proved the edge ships lifecycle +
1 Hz samples up to the center; M4 proves the center pushes **threshold
rules** down to the edge, the edge evaluates them **locally** (reusing the
shared `acquisition/services/alarms.py`), and a triggered alarm rides an
`alarm_event` frame back up — landing on the center `/alarms` page tagged
with the **source edge**.

Protocol contract: `protocol.md` v0.4 (`alarm_rules` on `apply_config` +
the `alarm_event` uplink frame, per-edge `monotonic_seq`).

Prerequisites: Docker + Docker Compose. Mock Modbus device is bundled in
`docker-compose.edge.yml` — no real PLC needed.

## 1. Start the center

```bash
docker compose -f docker-compose.center.yml up -d --build
until curl -sf localhost:8000/api/fleet/edges/ >/dev/null; do sleep 2; done
```

## 2. Register an edge

```bash
curl -s -X POST localhost:8000/api/fleet/edges/ \
  -H 'Content-Type: application/json' \
  -d '{"name": "edge-smoke-m4", "labels": {"site": "lab"}}'
# → note the EdgeNode "id" and the one-shot "activation_token"
```

## 3. Start the edge stack

```bash
export EDGE_ID=edge-smoke-m4
export EDGE_TOKEN=<activation_token from step 2>
export CENTER_URL=ws://<center-host-ip>:8000/ws/fleet/

docker compose -f docker-compose.edge.yml up -d --build
docker compose -f docker-compose.edge.yml logs -f edge-agent
```

The log shows `registered with center as edge=edge-smoke-m4 proto=0.4`.
Note `proto=0.4` — a v0.4 edge mirrors `alarm_rules` and emits
`alarm_event` frames.

## 4. Build + assign + push a Modbus task

Same as M2 smoke steps 4–5 (device `mock-modbus-edge:5020`, holding
points, AcqTask, `PATCH .../tasks/<id>/ {edge_id}`, then
`POST /api/fleet/edges/<edge_id>/assignments/sync/`). The edge applies the
snapshot and the pipeline starts reading. Note one point's `code` (e.g.
`holding_0`) and watch its live value:

```bash
curl -s 'localhost:8000/api/fleet/samples/?edge=<edge_id>' | python3 -m json.tool
```

## 5. Create a threshold rule at the center

Pick a threshold the mock device's `holding_0` **crosses** — read the live
value from step 4 and set the threshold below it (or `0` for a register
that is always non-zero).

```bash
curl -s -X POST localhost:8000/api/acquisition/alarm-rules/ \
  -H 'Content-Type: application/json' \
  -d '{"name": "holding_0 过高", "point_code": "holding_0",
       "operator": "gt", "threshold": 0, "severity": "warning",
       "is_active": true}'
```

Creating the rule fires the `post_save` signal → the center re-pushes
`apply_config` to every online edge. The center log shows:

```
fleet: alarm rules re-synced to 1 online edge(s)
```

and the edge log shows a fresh `apply_config` apply. Confirm the rule
reached the edge's local SQLite:

```bash
docker compose -f docker-compose.edge.yml exec -T edge-agent \
  sh -c 'cd /opt/backend && DJANGO_DB_NAME=/var/lib/edge-agent/edge_state.db \
   python -c "import django;django.setup();\
from acquisition.models import AlarmRule;\
print(list(AlarmRule.objects.values_list(\"id\",\"point_code\",\"threshold\")))"'
```

PASS criteria: the center logs the re-sync; the rule row exists in the
edge DB under the same `id` as the center.

## 6. Verify the alarm fired locally and reached the center

The edge's in-process `AlarmSink` evaluates `holding_0` against the rule
each read. When the value breaches the threshold it creates a local
`Alarm` row **and** emits an `alarm_event` frame. Within ~60 s:

```bash
curl -s 'localhost:8000/api/acquisition/alarms/?status=firing' | python3 -m json.tool
```

Expect at least one alarm with:

- `point_code: "holding_0"`, `status: "firing"`
- `edge: <edge_id>`  and  `edge_name: "edge-smoke-m4"`  ← **来源 edge 标注**
- `rule_name: "holding_0 过高"`

The center log shows the inbound frame:

```
fleet: alarm_event edge=<id> rule=<id> point=holding_0 value=<v> seq=<n> alarm=<pk>
```

Filter the `/alarms` page by source edge:

```bash
curl -s 'localhost:8000/api/acquisition/alarms/?edge=<edge_id>'
```

`EdgeNode.last_uplink_seq` keeps climbing — `alarm_event` shares the M3
uplink sequence stream with `lifecycle` / `sample_batch`.

PASS criteria: ≥1 `firing` alarm whose `edge_name` is `edge-smoke-m4`;
the center logs the `alarm_event` line.

## 7. Verify a threshold change auto re-deploys

Raise the threshold above the live value so the rule no longer matches,
then confirm the edge picked up the new threshold without a manual sync:

```bash
curl -s -X PATCH localhost:8000/api/acquisition/alarm-rules/<rule_id>/ \
  -H 'Content-Type: application/json' \
  -d '{"threshold": 999999}'
```

The `post_save` signal re-pushes `apply_config`; the center logs
`alarm rules re-synced`. Re-run the edge-DB query from step 5 — the
`threshold` column now reads `999999.0`. No **new** `firing` alarms appear
for `holding_0` after the change (the already-open alarm stays until M5
adds clear-transition uplink).

PASS criteria: the edge DB shows the updated threshold; no new alarms
fire under the raised threshold.

## 8. Verify the alarm rule is culled on delete

```bash
curl -s -X DELETE localhost:8000/api/acquisition/alarm-rules/<rule_id>/
```

The `post_delete` signal re-pushes `apply_config` (now with an empty
`alarm_rules`). The edge culls the rule — the step-5 edge-DB query returns
`[]`.

PASS criteria: the rule is gone from the edge DB after the delete.

## Teardown

```bash
docker compose -f docker-compose.edge.yml down       # add -v to wipe the
docker compose -f docker-compose.center.yml down     # cached apply_config
```

## Last verified

Unit suites green on branch `distributed/m4-alarms` ([XIU-69]):
backend `tests/test_fleet_m4.py` 16 passed + `test_fleet*` 43 passed;
edge-agent suite 46 passed. The Docker smoke steps 1–8 above are the
QA回归 checklist — to be run by the test engineer before integration
review (same as M3 [XIU-65]).

[XIU-69]: ../../  "distributed M4 alarms"
