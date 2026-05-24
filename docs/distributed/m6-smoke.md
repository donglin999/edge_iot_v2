# M6 smoke runbook — 历史回查代理 (XIU-83)

Mirror of the existing M2–M5 smoke flow, focused on the M6 deliverable:
center can serve historical samples for the `/data` page even though no
raw samples land on the center anymore.

## Topology

```
center ──┬── WS ──▶ edge-a (compose stack #1)  → local InfluxDB + Modbus mock
         └── WS ──▶ edge-b (compose stack #2)  → local InfluxDB + Modbus mock
```

Two edges on one host, each with its own docker-compose project name and
host-side history port. The center runs from `docker-compose.center.yml`.

## Pre-flight

```bash
git checkout distributed/m6-history-proxy
docker compose -f docker-compose.center.yml up -d --build
# Wait for the center to be healthy, then mint two edge tokens:
EDGE_A_TOKEN=$(curl -s -XPOST http://localhost:8000/api/fleet/edges/ \
  -H "Content-Type: application/json" \
  -d '{"name":"edge-a"}' | jq -r .token)
EDGE_B_TOKEN=$(curl -s -XPOST http://localhost:8000/api/fleet/edges/ \
  -H "Content-Type: application/json" \
  -d '{"name":"edge-b"}' | jq -r .token)
```

Tell the center to accept the same Bearer tokens for the proxy
(simplest: use the activation tokens as the proxy credentials too):

```bash
docker compose -f docker-compose.center.yml exec center bash -c \
  "export EDGE_HISTORY_PROXY_TOKENS='{\"edge-a\":\"$EDGE_A_TOKEN\",\"edge-b\":\"$EDGE_B_TOKEN\"}' && \
   pkill -HUP daphne"
# (Or recreate the center container with those tokens baked in via .env.)
```

## Bring up the two edges

```bash
# Edge A — host history port 18086
EDGE_ID=edge-a EDGE_TOKEN=$EDGE_A_TOKEN \
CENTER_URL=ws://host.docker.internal:8000/ws/fleet/ \
EDGE_HISTORY_HOST_PORT=18086 \
EDGE_HISTORY_URL=http://host.docker.internal:18086 \
docker compose -f docker-compose.edge.yml -p edge-a up -d --build

# Edge B — host history port 18087
EDGE_ID=edge-b EDGE_TOKEN=$EDGE_B_TOKEN \
CENTER_URL=ws://host.docker.internal:8000/ws/fleet/ \
EDGE_HISTORY_HOST_PORT=18087 \
EDGE_HISTORY_URL=http://host.docker.internal:18087 \
docker compose -f docker-compose.edge.yml -p edge-b up -d --build
```

Verify both edges show **online** on `/fleet`:

```bash
curl -s http://localhost:8000/api/fleet/edges/ | jq '.[] | {name,status}'
# Expected:
# {"name":"edge-a","status":"online"}
# {"name":"edge-b","status":"online"}
```

Confirm the edges advertised their history URL via labels:

```bash
curl -s http://localhost:8000/api/fleet/edges/ \
  | jq '.[] | {name, history_url: .labels.history_url}'
# Expected:
# {"name":"edge-a","history_url":"http://host.docker.internal:18086"}
# {"name":"edge-b","history_url":"http://host.docker.internal:18087"}
```

## Dispatch one task per edge

Create two AcqTasks, each bound to a separate edge. Quickest path is the
admin / Excel import; the smoke just needs two tasks `T_A` (→ `edge-a`)
and `T_B` (→ `edge-b`) each with at least one numeric point.

Wait ~5 minutes for the Modbus mock to fill both edge InfluxDBs.

## Acceptance scenarios

| # | Action | Expected |
|---|--------|----------|
| 1 | `/data` → select task `T_A` → open Drawer (近 5 分钟) | Chart renders. Drawer header shows `数据来源: edge-a`. |
| 2 | Switch to task `T_B` → Drawer | Chart renders. Header now shows `数据来源: edge-b`. |
| 3 | `docker compose -p edge-a stop edge-agent` → switch back to `T_A` | Drawer renders an explicit error: `edge-a offline`. No empty chart. |
| 4 | Restart `edge-a`, wait for online, then `docker compose -f docker-compose.center.yml stop influxdb` → query `T_A` history | Drawer still renders. The center proxy never touched its local Influx for fleet tasks. |
| 5 | Curl the center proxy directly: <br/> `curl 'http://localhost:8000/api/history/points?task_id=<T_A_id>&point_ids=Temperature_01&start=-5m'` | 200, `sources["edge-a"]` populated, `errors == {}`. |
| 6 | Stop `edge-a` again and curl the same URL | 503, `errors["edge-a"].code == "edge_offline"`. |
| 7 | `curl http://localhost:18086/healthz` (direct edge) | `{"status":"ok","edge_id":"edge-a"}`. |
| 8 | `curl http://localhost:18086/history/points?point_ids=p1` (no token) | 401 `missing_token`. |

## Tear-down

```bash
docker compose -f docker-compose.edge.yml -p edge-a down -v
docker compose -f docker-compose.edge.yml -p edge-b down -v
docker compose -f docker-compose.center.yml down -v
```

## Known gotchas

* The host needs a route to each edge's history port — for Linux hosts
  use the real network address instead of `host.docker.internal`.
* If `EDGE_HISTORY_URL` is omitted the edge advertises
  `http://<EDGE_ID>:18086`, which only works when the center can resolve
  `edge-a` / `edge-b` (i.e. they share a docker network). For a
  cross-host deployment set `EDGE_HISTORY_URL` explicitly to a routable
  address.
* The proxy uses `EDGE_HISTORY_PROXY_DEFAULT_TOKEN` first if set; if a
  per-edge map is supplied via `EDGE_HISTORY_PROXY_TOKENS` the per-edge
  entry wins for that edge only.
