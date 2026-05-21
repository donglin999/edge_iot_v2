# Distributed control-plane protocol — v0.2 (M2)

Wire format for the WebSocket channel between each **edge-agent** and the
**center** (`fleet/` Django app). One persistent WS connection per edge.

- Transport: WebSocket text frames, JSON-encoded object per frame.
- Endpoint: `ws[s]://<center>/ws/fleet/`
- Authentication: activation token, presented in the first `register` frame.

Every frame carries `"v": "0.2"`. The center also accepts `"v": "0.1"`
during the upgrade window so a stale agent can still register; older
agents simply never see the new v0.2 frame types.

## Common envelope

| field   | type     | required | notes                                              |
|---------|----------|----------|----------------------------------------------------|
| `v`     | string   | yes      | Protocol version, e.g. `"0.2"`. Bump on breaking change. |
| `type`  | string   | yes      | See per-direction tables below.                    |

Per-type fields below.

## v0.1 frames (unchanged)

These exist exactly as defined in v0.1. Listed here in short form; see
the v0.1 history for full schemas.

| frame       | direction        | purpose                                                          |
|-------------|------------------|------------------------------------------------------------------|
| `register`  | edge → center    | First frame; presents `edge_id` + activation `token` + `version`.|
| `heartbeat` | edge → center    | ~1 Hz liveness; updates `last_seen`.                             |
| `ack`       | center → edge    | Acks a `register` / `heartbeat` / `apply_config` / `task_state`. |
| `error`     | center → edge    | Fatal protocol/auth error; center closes the socket.             |

`ack` now carries an optional `ref` matching the acked frame's `type`
(e.g. `ref="apply_config"`).

The error codes for `register` (`bad_frame` / `auth_failed` /
`unknown_edge`, closed with 4400/4401/4404) are unchanged.

## `apply_config` — center → edge  (v0.2)

Center pushes the full configuration snapshot the edge should run. The
center may resend any time (config edit, edge reconnect, periodic
reconcile). The edge MUST treat each frame as a full replacement of its
local state — anything not in `tasks` is no longer assigned to it.

```json
{
  "v": "0.2",
  "type": "apply_config",
  "version": 7,
  "tasks": [
    {
      "id": 12,
      "code": "modbus-line-1",
      "name": "Modbus Line 1",
      "sample_rate_hz": 1.0,
      "is_active": true,
      "point_ids": [101, 102, 103]
    }
  ],
  "devices": [
    {
      "id": 5,
      "code": "modbus-tcp-1",
      "name": "Modbus TCP #1",
      "protocol": "modbus_tcp",
      "ip_address": "mock-modbus-edge",
      "port": 5020,
      "metadata": {}
    }
  ],
  "points": [
    {
      "id": 101,
      "device_id": 5,
      "code": "holding_0",
      "address": "40001",
      "sample_rate_hz": 1.0,
      "extra": {"register_type": "holding", "data_type": "uint16"},
      "template": {"name": "Holding 0", "unit": "", "data_type": "uint16",
                   "coefficient": 1.0, "precision": 0}
    }
  ]
}
```

| field      | type    | required | notes                                                                |
|------------|---------|----------|----------------------------------------------------------------------|
| `version`  | integer | yes      | Monotonically increasing per edge. Persisted as `last_applied_version`. |
| `tasks`    | array   | yes      | All `AcqTask`s currently assigned to this edge (`AcqTask.edge_id == edge.id`). May be empty. |
| `devices`  | array   | yes      | All `Device`s referenced by `tasks[*].point_ids`. Joined transitively. |
| `points`   | array   | yes      | All `Point`s referenced by `tasks[*].point_ids`. Carries inline `template`. |

Each task object's fields are: `id`, `code`, `name`, `sample_rate_hz`,
`is_active`, `point_ids` (list of `Point.id`). Each device follows the
existing `DeviceSerializer` shape (`id`, `code`, `name`, `protocol`,
`ip_address`, `port`, `metadata`). Each point carries `id`, `device_id`,
`code`, `address`, `sample_rate_hz`, `extra`, plus an inline `template`
sub-object so the edge has everything it needs to run without a follow-up
fetch.

## `config_applied` — edge → center  (v0.2)

Reports the result of applying a snapshot. Sent once per `apply_config`.

```json
{
  "v": "0.2",
  "type": "config_applied",
  "edge_id": "edge-shanghai-line-1",
  "version": 7,
  "status": "ok"
}
```

On failure:

```json
{
  "v": "0.2",
  "type": "config_applied",
  "edge_id": "edge-shanghai-line-1",
  "version": 7,
  "status": "error",
  "error": "device protocol 'mqtt' not yet supported on edge"
}
```

| field      | type    | required | notes                                                  |
|------------|---------|----------|--------------------------------------------------------|
| `version`  | integer | yes      | Echoes back the `version` from the `apply_config` frame. |
| `status`   | string  | yes      | `"ok"` or `"error"`.                                    |
| `error`    | string  | no       | Short description; required when `status == "error"`.   |

## `task_state` — edge → center  (v0.2)

Reports a lifecycle transition for one running task. Sent on every state
change; the edge MUST NOT send `task_state` for a task that is not (or
no longer) in its local task list.

```json
{
  "v": "0.2",
  "type": "task_state",
  "edge_id": "edge-shanghai-line-1",
  "task_id": 12,
  "task_code": "modbus-line-1",
  "state": "running"
}
```

| field        | type    | required | notes                                                         |
|--------------|---------|----------|---------------------------------------------------------------|
| `task_id`    | integer | yes      | `AcqTask.id`, exactly as received in `apply_config.tasks[*]`. |
| `task_code`  | string  | yes      | `AcqTask.code`, included for logs.                            |
| `state`      | string  | yes      | One of `starting`, `running`, `stopping`, `stopped`, `error`. |
| `error`      | string  | no       | Required when `state == "error"`; short description.          |

The center persists the latest state per (edge, task) into the
`EdgeTaskStatus` table and exposes it via the existing
`/api/acquisition/` page so the operator sees "task X on edge Y → running".

## Frame ordering / acks

| frame            | acked by center?                       |
|------------------|----------------------------------------|
| `register`       | `ack` with `ref="register"`            |
| `heartbeat`      | `ack` with `ref="heartbeat"`           |
| `apply_config`   | (center → edge; no ack from center)    |
| `config_applied` | `ack` with `ref="config_applied"`      |
| `task_state`     | `ack` with `ref="task_state"`          |

The edge MAY ignore acks for `task_state` / `config_applied`; they exist
for at-least-once delivery hooks the center may grow later.

## Reconnect behaviour (client side)

Unchanged from v0.1:

- On any close that isn't `1000`/`1001`, the edge waits an
  exponential-backoff delay (1 → 2 → 4 → 8 → 16 → 30 s, capped at 30 s)
  and reconnects.
- The backoff timer is **reset to 1 s** after the next successful
  `register` ack, so a transient blip does not keep the agent at
  30 s indefinitely.
- On reconnect the center re-pushes the latest `apply_config` so the
  edge converges back to the desired state without polling.

## Out of scope for v0.2 (planned for M3+)

- `sample` / `sample_batch` (edge → center 1Hz aggregated readings) — M3
- `alarm` rule sync + `alarm_event` (edge → center) — M4
- Offline buffering / backfill — M5
- Historical query proxy — M6
- mTLS / signed bootstrap, beyond the bare activation token
