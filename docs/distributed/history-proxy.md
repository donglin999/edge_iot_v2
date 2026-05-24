# M6 — 历史回查代理 (`history-proxy`)

> Status: shipped on `distributed/m6-history-proxy` (XIU-83).
> Protocol version: HTTP, **independent of WS protocol** (`protocol.md`).

In fleet mode the raw acquisition samples only live in the **edge-side
InfluxDB** — the center never receives them as part of the steady-state
uplink. M6 closes the read path so the existing `/data` Drawer
(5m/1h/6h/24h history) keeps working: the center exposes a thin proxy
that, on demand, fans out to each affected edge's read-only HTTP
endpoint and returns merged results.

This is an HTTP contract, not a WS frame. Adding it does **not** bump
`PROTOCOL_VERSION` (M5's `v0.5`); the WS protocol is unchanged.

---

## Topology

```
browser ──HTTP──▶ center  /api/history/points
                    │
                    ├──HTTP──▶ edge-a  :18086/history/points  (Bearer token)
                    ├──HTTP──▶ edge-b  :18086/history/points
                    └──…
```

* The browser **never** talks to the edge directly — only the center
  proxy holds the bearer token and the edge URL list.
* The edge HTTP server stays up across center-WS reconnects: a transient
  WS drop does not take history queries down with it.
* The center never persists raw samples; every query hits live edge
  Influx. Acceptance scenario: shut the **center** Influx down — history
  queries still succeed.

---

## Endpoint — edge

### `GET /history/points`

Read-only Flux query against the edge's local InfluxDB.

**Auth:** `Authorization: Bearer <EDGE_TOKEN>` — same token the edge
agent uses for the WS register flow. Missing → `401 missing_token`;
mismatched → `401 bad_token`.

**Query parameters**

| Name        | Required | Default  | Notes |
|-------------|----------|----------|-------|
| `point_ids` | ✓        | —        | Comma-separated point codes (max 200). |
| `start`     |          | `-5m`    | Relative (`-5m`, `-1h`), RFC3339, or `now()`. |
| `end`       |          | `now()`  | Same shapes as `start`. |
| `agg`       |          | `raw`    | One of `raw` / `1s` / `10s`. |
| `limit`     |          | 5000     | Hard-capped at `HISTORY_MAX_POINTS` (default `50000`). |

**Response 200**

```json
{
  "edge_id": "edge-shanghai-1",
  "point_ids": ["Temperature_01"],
  "start": "-1h",
  "end": "now()",
  "agg": "raw",
  "limit": 5000,
  "count": 3600,
  "truncated": false,
  "elapsed_ms": 38,
  "data": [
    {"point_code": "Temperature_01",
     "timestamp": "2026-01-01T00:00:00+00:00",
     "value": 25.6,
     "quality": "good"}
  ]
}
```

**Error responses**

| Status | `code`                | When |
|--------|-----------------------|------|
| 400    | `missing_point_ids`   | No `point_ids`. |
| 400    | `bad_agg`             | `agg` not in {raw, 1s, 10s}. |
| 400    | `bad_limit`           | `limit` not a positive integer. |
| 400    | `too_many_point_ids`  | > 200 codes per request. |
| 401    | `missing_token`       | No `Authorization` header. |
| 401    | `bad_token`           | Token mismatch. |
| 500    | `query_failed`        | Flux query raised. |
| 503    | `influx_unavailable`  | Edge InfluxDB not reachable. |

### `GET /healthz`

Unauthenticated liveness probe used by docker-compose healthchecks and
the center proxy fast-fail. Returns `{"status":"ok","edge_id":"…"}`.

### Configuration

| Env                          | Default          | Purpose |
|------------------------------|------------------|---------|
| `EDGE_HISTORY_ENABLED`       | `true`           | Disable the server entirely. |
| `EDGE_HISTORY_HOST`          | `0.0.0.0`        | Bind address. |
| `EDGE_HISTORY_PORT`          | `18086`          | Bind port. Exposed to the center, **not** to public networks. |
| `EDGE_HISTORY_MAX_POINTS`    | `50000`          | Hard cap on the Flux `limit`. |
| `EDGE_HISTORY_URL`           | `http://$EDGE_ID:18086` | Externally reachable URL the center calls. Mirrored into `labels['history_url']` on register so the center can discover it without a side-channel. |

---

## Endpoint — center

### `GET /api/history/points`

Resolves each `task_id` to its owning edge, groups requested
`point_ids` per edge, fan-outs **one HTTP call per edge** in parallel,
and merges the responses.

**Query parameters**

| Name        | Required | Default | Notes |
|-------------|----------|---------|-------|
| `task_id`   | ✓        | —       | Comma-separated `AcqTask.id`s. |
| `point_ids` | ✓        | —       | Forwarded to every targeted edge. |
| `start`     |          | `-5m`   | See edge endpoint. |
| `end`       |          | `now()` | See edge endpoint. |
| `agg`       |          | `raw`   | See edge endpoint. |
| `limit`     |          | 5000    | Per-edge cap (edge enforces its own ceiling on top). |
| `timeout`   |          | 10      | Per-edge HTTP timeout, seconds. Clamped to `[0.5, 30]`. |

Tasks whose `AcqTask.edge` is `NULL` fall through to a direct query
against the center's InfluxDB so legacy single-host deployments keep
working unchanged.

**Response 200 (full success and partial success)**

```jsonc
{
  "task_ids": [1, 2],
  "point_ids": ["Temperature_01"],
  "start": "-1h",
  "end": "now()",
  "agg": "raw",
  "limit": 5000,
  "count": 7200,
  "queried_at": "2026-05-24T10:00:00+00:00",
  "sources": {
    "edge-a": { /* same shape as the edge response */ },
    "edge-b": { /* … */ }
  },
  "errors": {
    "edge-stale": {
      "edge_id": "edge-stale",
      "status": 503,
      "code": "edge_offline",
      "message": "edge 'edge-stale' is offline (last_seen=2026-05-24T09:58:00+00:00)"
    }
  },
  "data": [
    {"point_code": "Temperature_01",
     "timestamp": "2026-01-01T00:00:00+00:00",
     "value": 25.6,
     "quality": "good",
     "edge_id": "edge-a"}
  ]
}
```

The merged `data` array stamps every datum with its source `edge_id`
so the UI can render *which* edge served *which* sample.

**Response 503**

Returned when **every** targeted edge failed (no successful `sources`).
The body still includes `errors[edge_id]` so the UI shows a precise
"edge X offline" message.

**Per-edge `errors[].code`**

| Code                  | HTTP code in `status` | Meaning |
|-----------------------|----------------------|---------|
| `edge_offline`        | 503                  | `EdgeNode.is_stale()` — last heartbeat > 30 s ago. |
| `edge_url_unknown`    | 503                  | Online but advertised no `history_url` and no override. |
| `edge_timeout`        | 504                  | HTTP timeout (default 10 s). |
| `edge_unreachable`    | 502                  | Connection refused / DNS failure. |
| `edge_auth_rejected`  | 502                  | Edge replied 401 to the proxy token. |
| `edge_http_error`     | 502 / forwarded      | Edge replied 4xx/5xx (body forwarded, truncated to 200 chars). |
| `edge_bad_json`       | 502                  | Edge replied non-JSON. |
| `edge_token_missing`  | 500                  | Proxy not configured with a token for this edge. |

### Center configuration

| Setting                            | Source             | Purpose |
|------------------------------------|--------------------|---------|
| `EDGE_HISTORY_PROXY_DEFAULT_TOKEN` | env                | Shared-secret Bearer token for *all* edges (smoke / dev). |
| `EDGE_HISTORY_PROXY_TOKENS`        | env (JSON)         | Per-edge map `{edge_name: token}`; overrides the default. |
| `EDGE_HISTORY_PROXY_URLS`          | env (JSON)         | Per-edge URL override `{edge_name: base_url}`. Only used when the edge can't advertise its own URL (NAT, double-network). Labels still win when present. |
| `EDGE_HISTORY_PROXY_TIMEOUT_S`     | env                | Default per-edge timeout. Caller's `?timeout=` overrides. |

### URL resolution order

1. `EdgeNode.labels['history_url']` — set by the edge in its register
   frame (M6 convention).
2. `EDGE_HISTORY_PROXY_URLS[<edge_name>]` — operator override.
3. → `edge_url_unknown` 503.

### Token policy

The center stores only the **salted hash** of each edge's register
token (`EdgeNode.token_hash`). For the proxy we therefore use a
*separate* read-only Bearer credential, supplied to the center via
`EDGE_HISTORY_PROXY_DEFAULT_TOKEN` (single shared secret) or
`EDGE_HISTORY_PROXY_TOKENS` (per-edge mapping). The edge accepts the
same value it has in `EDGE_TOKEN` — there is no separate edge-side
read-only secret in M6; the smoke compose simply reuses the activation
token. Production deployments that want a read-only credential can set
`EDGE_TOKEN` to the proxy credential on the edge and keep the
activation token in `EDGE_HISTORY_PROXY_TOKENS` on the center.

---

## Security notes

* The edge HTTP server binds `0.0.0.0:18086` by default. **Do not
  publish this port to the internet** — it sits on the same trusted
  network as the WS link (typically a private subnet between center
  and edges).
* Token mismatch returns `401`. There is no rate-limit on `/history/points`
  in M6 (the trust boundary is the network, not the protocol); add one
  before exposing the port more broadly.
* `point_ids` and `start/end` are escaped before going into Flux —
  injection-safe inside the double-quoted literals.
* `HISTORY_MAX_POINTS` (default 50 000) caps every response so a
  misbehaving caller can't OOM the agent.
