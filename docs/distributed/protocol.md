# Distributed control-plane protocol — v0.1 (M1)

Wire format for the WebSocket channel between each **edge-agent** and the
**center** (`fleet/` Django app). One persistent WS connection per edge.

- Transport: WebSocket text frames, JSON-encoded object per frame.
- Endpoint: `ws[s]://<center>/ws/fleet/`
- Authentication: activation token, presented in the first `register` frame.

Every frame carries `"v": "0.1"` so the center can reject (or, later,
shim) frames from agents speaking a different version.

## Common envelope

| field   | type     | required | notes                                              |
|---------|----------|----------|----------------------------------------------------|
| `v`     | string   | yes      | Protocol version, e.g. `"0.1"`. Bump on breaking change. |
| `type`  | string   | yes      | One of: `register`, `heartbeat`, `ack`, `error`.   |

Per-type fields below.

## `register` — edge → center

First frame after the WS opens. The edge MUST NOT send `heartbeat` until
it has received an `ack` for its `register`.

```json
{
  "v": "0.1",
  "type": "register",
  "edge_id": "edge-shanghai-line-1",
  "token": "<activation token from POST /api/fleet/edges/>",
  "version": "0.1.0",
  "labels": {"site": "shanghai", "line": "1"}
}
```

| field      | type     | required | notes                                                     |
|------------|----------|----------|-----------------------------------------------------------|
| `edge_id`  | string   | yes      | Matches `EdgeNode.name` on the center.                    |
| `token`    | string   | yes      | The plaintext activation token; compared to `token_hash`. |
| `version`  | string   | yes      | edge-agent semver. Stored on `EdgeNode.version`.          |
| `labels`   | object   | no       | Free-form `{string: string}`; replaces server-side value. |

On success the center responds with:

```json
{ "v": "0.1", "type": "ack", "ref": "register" }
```

On failure the center sends an `error` and closes the socket with one of:

- `4400` — `bad_frame` (malformed JSON / missing fields)
- `4401` — `auth_failed` (token didn't verify)
- `4404` — `unknown_edge` (no `EdgeNode` with that name)

## `heartbeat` — edge → center

Sent every ~1 s after a successful register.

```json
{
  "v": "0.1",
  "type": "heartbeat",
  "edge_id": "edge-shanghai-line-1",
  "cpu": 0.0,
  "mem": 0.0,
  "uptime": 12.3,
  "tasks": 0
}
```

| field      | type   | required | notes                                                  |
|------------|--------|----------|--------------------------------------------------------|
| `edge_id`  | string | yes      | Sanity check; must match the registered edge.          |
| `cpu`      | number | no       | 0.0–1.0, fraction of single core. M1 sends `0.0`.      |
| `mem`      | number | no       | 0.0–1.0, fraction of RAM. M1 sends `0.0`.              |
| `uptime`   | number | no       | Seconds since agent process start.                     |
| `tasks`    | number | no       | Count of acquisition tasks running. M1 sends `0`.      |

Each heartbeat is acked:

```json
{ "v": "0.1", "type": "ack", "ref": "heartbeat" }
```

The center treats any edge whose `last_seen` is older than **30 s** as
`offline`. The sweep runs on every `GET /api/fleet/edges/` and (M2+) on a
background Celery beat.

## `ack` — center → edge

```json
{ "v": "0.1", "type": "ack", "ref": "register" }
```

| field   | type   | required | notes                                                  |
|---------|--------|----------|--------------------------------------------------------|
| `ref`   | string | no       | The `type` of the frame this acks. Useful for tracing. |

## `error` — center → edge

```json
{
  "v": "0.1",
  "type": "error",
  "code": "auth_failed",
  "message": "invalid token"
}
```

| field     | type   | required | notes                                                              |
|-----------|--------|----------|--------------------------------------------------------------------|
| `code`    | string | yes      | Machine-readable. Values: `bad_frame`, `auth_failed`, `unknown_edge`. |
| `message` | string | no       | Human-readable detail.                                             |

The center always closes the socket immediately after sending an
`error` frame (using the 44xx close code matching `code`).

## Reconnect behaviour (client side)

- On any close that isn't `1000`/`1001`, the edge waits an
  exponential-backoff delay (1 → 2 → 4 → 8 → 16 → 30 s, capped at 30 s)
  and reconnects.
- The backoff timer is **reset to 1 s** after the next successful
  `register` ack, so a transient blip does not keep the agent at
  30 s indefinitely.

## Out of scope for v0.1 (planned for M2+)

- `cmd` / `cmd_ack` (center → edge config & task control)
- `sample` / `sample_batch` (edge → center aggregated readings)
- `alarm` (edge → center alarm events)
- mTLS / signed bootstrap, beyond the bare activation token
