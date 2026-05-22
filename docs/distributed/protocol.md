# Distributed control-plane protocol — v0.3 (M3)

Wire format for the WebSocket channel between each **edge-agent** and the
**center** (`fleet/` Django app). One persistent WS connection per edge.

- Transport: WebSocket text frames, JSON-encoded object per frame.
- Endpoint: `ws[s]://<center>/ws/fleet/`
- Authentication: activation token, presented in the first `register` frame.

Every frame carries `"v": "0.3"`. The center also accepts `"v": "0.1"`
and `"v": "0.2"` during the upgrade window so a stale agent can still
register; older agents simply never see the newer frame types.

## Common envelope

| field   | type     | required | notes                                              |
|---------|----------|----------|----------------------------------------------------|
| `v`     | string   | yes      | Protocol version, e.g. `"0.3"`. Bump on breaking change. |
| `type`  | string   | yes      | See per-direction tables below.                    |

Per-type fields below.

## v0.1 / v0.2 frames (unchanged)

These exist exactly as defined in their introducing version. v0.3 is a
**pure increment** — it adds two new edge → center frame types and does
not change any v0.1/v0.2 field. The M1/M2 public contract is preserved.

| frame            | direction        | version | purpose                                       |
|------------------|------------------|---------|-----------------------------------------------|
| `register`       | edge → center    | v0.1    | First frame; `edge_id` + `token` + `version`. |
| `heartbeat`      | edge → center    | v0.1    | ~1 Hz liveness; updates `last_seen`.          |
| `ack`            | center → edge    | v0.1    | Acks a prior edge → center frame.             |
| `error`          | center → edge    | v0.1    | Fatal protocol/auth error; center closes.     |
| `apply_config`   | center → edge    | v0.2    | Full task/device/point snapshot.              |
| `config_applied` | edge → center    | v0.2    | Result of applying a snapshot.                |
| `task_state`     | edge → center    | v0.2    | Per-task lifecycle transition (no seq).       |

`task_state` is **retained** so a v0.2 edge talking to a v0.3 center keeps
working. A v0.3 edge no longer emits `task_state`; it emits the richer
`lifecycle` frame instead (see below). The center accepts both.

See this file's git history for the full v0.1/v0.2 field schemas of the
frames above.

## Uplink sequencing — `monotonic_seq`

v0.3 introduces a single per-edge **uplink sequence number**. Every
`lifecycle` and `sample_batch` frame carries `monotonic_seq`: a strictly
increasing integer (`>= 1`) assigned by the edge-agent at enqueue time,
shared across both frame types so they form one ordered stream.

Edge side:

- The counter starts at `1` on each edge-agent **process start** and
  increments by 1 per uplink frame. It is per-process: it keeps counting
  across WS reconnects within the same process; a process restart restarts
  it at `1`. It is not persisted to disk this milestone.

Center side:

- The center stores the highest value it has seen per edge as
  `EdgeNode.last_uplink_seq`, and **resets it to `0` on every `register`**.
  A new WS session is a new uplink stream — a fresh socket means the prior
  high-water mark no longer applies.
- The **first frame after a register** (`last_uplink_seq == 0`) is always
  accepted as `advanced`, whatever its `monotonic_seq`. This covers both a
  cold start (the edge sends seq `1`) and a reconnect of a long-running
  agent (the edge's per-process counter is already at seq `N`). Without
  this, a short-session restart — agent emits a few frames, restarts, new
  stream from seq `1` — would see seq `1..N` mis-classified as stale
  duplicates and dropped.
- Within an established stream (`last_uplink_seq > 0`):
  - `monotonic_seq <= last_uplink_seq` is a **duplicate** / replay: the
    center applies it idempotently and does **not** move the high-water
    mark backward.
  - `monotonic_seq == last_uplink_seq + 1` **advances** the stream.
  - `monotonic_seq > last_uplink_seq + 1` is a **gap** (frames lost while
    offline): the center logs it, still records the frame, and advances
    the mark. Closing the gap is M5 (offline backfill) — out of scope
    here, which only implements the online path.
- A frame with `monotonic_seq <= 0` is malformed and rejected with a
  `bad_frame` error.

`register` / `heartbeat` / `config_applied` / `task_state` do **not**
carry `monotonic_seq` — they are control frames, not part of the uplink
data stream.

## `lifecycle` — edge → center  (v0.3)

Reports a session- or task-level lifecycle event. Supersedes `task_state`
for v0.3 edges. Sent on every state change.

```json
{
  "v": "0.3",
  "type": "lifecycle",
  "edge_id": "edge-shanghai-line-1",
  "monotonic_seq": 7,
  "event": "task.running",
  "ts": "2026-05-22T03:14:07.221Z",
  "task_id": 12,
  "task_code": "modbus-line-1"
}
```

| field           | type    | required | notes                                                          |
|-----------------|---------|----------|-----------------------------------------------------------------|
| `monotonic_seq` | integer | yes      | Per-edge uplink sequence (see above).                           |
| `event`         | string  | yes      | One of the event names below.                                  |
| `ts`            | string  | yes      | Edge wall-clock ISO-8601 UTC; informational (clock may drift).  |
| `task_id`       | integer | cond.    | Required for every `task.*` event; `AcqTask.id`.                |
| `task_code`     | string  | cond.    | Required for every `task.*` event; human-readable code.         |
| `error`         | string  | cond.    | Required when `event == "task.error"`; short description.       |

`event` values:

| event             | scope   | meaning                                               |
|-------------------|---------|-------------------------------------------------------|
| `session.online`  | session | Edge-agent has registered; control plane is up.       |
| `task.starting`   | task    | Runner is spawning the task's acquisition thread.     |
| `task.running`    | task    | Acquisition service is reading.                       |
| `task.stopping`   | task    | Runner asked the task to stop.                        |
| `task.stopped`    | task    | Task thread exited cleanly.                           |
| `task.error`      | task    | Task crashed or failed to start; see `error`.         |

The `task.*` events map 1:1 onto the v0.2 `task_state` states, so the
center folds them into the same `EdgeTaskStatus` row (the `state` column
stores the part after `task.`). The center also appends every `lifecycle`
frame to an `EdgeLifecycleEvent` log table for an audit timeline.

`session.offline` is **not** an uplink event — a closing socket cannot
reliably send a final frame. The center synthesises a `session.offline`
row in `EdgeLifecycleEvent` itself when the WS disconnects.

## `sample_batch` — edge → center  (v0.3)

Carries one aggregation window of sampled point values. The edge-agent
aggregates the acquisition pipeline's readings over a fixed window
(default 1 Hz, latest value wins per `point_code`) and ships one
`sample_batch` per window per task.

```json
{
  "v": "0.3",
  "type": "sample_batch",
  "edge_id": "edge-shanghai-line-1",
  "monotonic_seq": 8,
  "task_id": 12,
  "task_code": "modbus-line-1",
  "window_start": "2026-05-22T03:14:06.000Z",
  "window_end": "2026-05-22T03:14:07.000Z",
  "samples": [
    {"point_code": "holding_0", "value": 123.4, "quality": "good",
     "timestamp": "2026-05-22T03:14:06.880Z"},
    {"point_code": "holding_1", "value": 7,     "quality": "good",
     "timestamp": "2026-05-22T03:14:06.880Z"}
  ]
}
```

| field           | type    | required | notes                                                       |
|-----------------|---------|----------|--------------------------------------------------------------|
| `monotonic_seq` | integer | yes      | Per-edge uplink sequence (see above).                        |
| `task_id`       | integer | yes      | `AcqTask.id` the samples belong to.                          |
| `task_code`     | string  | yes      | Human-readable task code.                                    |
| `window_start`  | string  | yes      | ISO-8601 UTC start of the aggregation window.                |
| `window_end`    | string  | yes      | ISO-8601 UTC end of the aggregation window.                  |
| `samples`       | array   | yes      | Aggregated readings; may be empty (a quiet window).          |

Each `samples[*]` object: `point_code` (string), `value` (number / bool /
string), `quality` (`good` / `bad` / `uncertain`), `timestamp` (ISO-8601
UTC of the underlying reading).

The center writes each sample into its **aggregation cache**
(`EdgeSample`, one row per (edge, task, point_code), updated in place) and
— when `CENTER_EDGE_SAMPLE_TO_INFLUX` is enabled (default on) — also into
InfluxDB under measurement `edge_sample`. The InfluxDB copy is meant for a
short-retention bucket (default 7 days, set as the bucket's retention
policy); it can be disabled entirely by setting
`CENTER_EDGE_SAMPLE_TO_INFLUX=false`.

### Aggregation window — edge-agent configuration

| env var                    | default | meaning                                              |
|-----------------------------|---------|------------------------------------------------------|
| `EDGE_UPLINK_SAMPLES`       | `true`  | Master switch for `sample_batch` uplink.             |
| `EDGE_UPLINK_SAMPLE_WINDOW` | `1.0`   | Aggregation window in seconds (≥ 0.05).              |

Setting `EDGE_UPLINK_SAMPLES=false` stops the edge emitting `sample_batch`
frames entirely (lifecycle frames are unaffected).

## Frame ordering / acks

| frame            | acked by center?                       |
|------------------|----------------------------------------|
| `register`       | `ack` with `ref="register"`            |
| `heartbeat`      | `ack` with `ref="heartbeat"`           |
| `apply_config`   | (center → edge; no ack from center)    |
| `config_applied` | `ack` with `ref="config_applied"`      |
| `task_state`     | `ack` with `ref="task_state"`          |
| `lifecycle`      | `ack` with `ref="lifecycle"`           |
| `sample_batch`   | `ack` with `ref="sample_batch"`        |

The edge MAY ignore acks for `lifecycle` / `sample_batch`; they exist for
at-least-once delivery hooks the center may grow later (M5).

## Reconnect behaviour (client side)

Unchanged from v0.1/v0.2:

- On any close that isn't `1000`/`1001`, the edge waits an
  exponential-backoff delay (1 → 2 → 4 → 8 → 16 → 30 s, capped at 30 s)
  and reconnects.
- The backoff timer is **reset to 1 s** after the next successful
  `register` ack.
- On reconnect the center re-pushes the latest `apply_config`.
- Uplink frames produced while the socket is down are buffered in a
  bounded in-process queue and flushed on reconnect (online path only;
  durable cross-restart buffering / backfill is M5).

## Out of scope for v0.3 (planned for M4+)

- `alarm` rule sync + `alarm_event` (edge → center) — M4
- Durable offline buffering / `monotonic_seq` gap backfill — M5
- Historical query proxy — M6
- mTLS / signed bootstrap, beyond the bare activation token
