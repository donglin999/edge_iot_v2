# Distributed control-plane protocol — v0.6

**Status: STABLE** — M7 (XIU-96) froze v0.5; v0.6 (XIU-112) is an
additive/clarifying bump that makes the **retained `edge/<id>/lwt` MQTT
topic the single source of truth for edge presence** (see
[Presence — `lwt` single source of truth](#presence--lwt-single-source-of-truth-v06-xiu-112)).
No wire frame changed shape. Future evolution stays additive-only.

Wire format for the channel between each **edge-agent** and the
**center** (`fleet/` Django app). Phase 1 ran one persistent WebSocket per
edge; Phase 2 (XIU-51) migrated the transport to **MQTT** (Mosquitto). The
frame catalogue below is transport-agnostic — the same JSON objects ride
either channel — except presence, which is MQTT-native (below).

- Transport: JSON-encoded object per frame (WS text frame, or MQTT publish
  payload on the per-edge topics).
- Endpoint (WS, legacy): `ws[s]://<center>/ws/fleet/`
- Presence (MQTT): retained `edge/<id>/lwt`, last-will configured per edge.
- Authentication: activation token, presented in the first `register` frame.

Every frame carries `"v": "0.5"`. The center also accepts `"v": "0.1"`
through `"v": "0.4"` during the upgrade window so a stale agent can still
register; older agents simply never see the newer frame types / fields.

## Frame catalogue (v0.5, stable)

| direction | type | first seen | required fields | notes |
|---|---|---|---|---|
| edge → center | `register` | v0.1 | `edge_id`, `token`, `version` | v0.5 adds optional `uplink_seq`. First frame on the WS. |
| edge → center | `heartbeat` | v0.1 | — | ~1 Hz liveness. v0.5 adds optional `buffer` (outbox depth). |
| edge → center | `task_state` | v0.2 | `task_id`, `state` | v0.2 lifecycle. v0.3+ edges emit richer `lifecycle` instead; center accepts both. |
| edge → center | `config_applied` | v0.2 | `version`, `accepted`, `summary` | Result of an `apply_config`. |
| edge → center | `lifecycle` | v0.3 | `task_id`, `state`, `monotonic_seq` | Per-task lifecycle transitions; carries uplink seq. |
| edge → center | `sample_batch` | v0.3 | `task_id`, `point_id`, `samples[]`, `monotonic_seq` | 1Hz / N-Hz aggregate batch from the edge pipeline. v0.5 may add `backfill: true`. |
| edge → center | `alarm_event` | v0.4 | `rule_id` or `rule_name`, `point_code`, `value`, `status`, `monotonic_seq` | `status ∈ {firing, cleared}`. v0.5 lights up `cleared`. |
| center → edge | `ack` | v0.1 | `ref_type`, `ref_seq` or `ref_msg_id` | Always sent in reply to an edge frame. v0.5 adds `last_uplink_seq`. |
| center → edge | `error` | v0.1 | `code`, `message` | Fatal; center closes the WS. |
| center → edge | `apply_config` | v0.2 | `version`, `tasks[]`, `devices[]`, `points[]` | Full snapshot. v0.4 adds optional `alarm_rules[]`. |

The wire schemas of each frame are documented in detail below.

## Common envelope

| field   | type     | required | notes                                              |
|---------|----------|----------|----------------------------------------------------|
| `v`     | string   | yes      | Protocol version, e.g. `"0.3"`. Bump on breaking change. |
| `type`  | string   | yes      | See per-direction tables below.                    |

Per-type fields below.

## Version increments (unchanged frames)

Every version bump on this protocol is a **pure increment** — a new
version adds frame types or *optional* fields and never changes an
existing field's meaning. The M1/M2/M3 public contract is preserved:

- **v0.3** added two edge → center frame types (`lifecycle`,
  `sample_batch`) and the per-edge `monotonic_seq` uplink counter.
- **v0.4** adds one edge → center frame type (`alarm_event`) and one
  *optional* field on the existing `apply_config` frame (`alarm_rules`).
  A v0.3 edge ignores `alarm_rules`; a v0.4 center sending it to a v0.3
  edge is harmless.
- **v0.5** adds **no new frame types** — only *optional* fields, for
  offline degradation + reconnect backfill (M5):
  - `register.uplink_seq` — the edge's persistent uplink high-water mark.
  - `ack.last_uplink_seq` — the center's high-water mark, returned on
    every ack so the edge knows where to backfill from / prune to.
  - `heartbeat.buffer` — depth of the edge's durable outbox.
  - `backfill: true` — set on a replayed uplink frame the edge is
    re-sending from its durable outbox after a reconnect.
  A v0.4 peer ignores every one of these fields.

  v0.5 also **lights up** `alarm_event.status: "cleared"` — the field has
  been on the wire (reserved) since v0.4, so this is not a frame change.
  The edge now emits an `alarm_event` on a clear transition as well as a
  fire; the center closes the matching open `Alarm`. No existing field
  changes meaning.
- **v0.6** adds **no new frame types and changes no field shape**. It
  re-specifies *how the center derives edge presence*: the retained
  `edge/<id>/lwt` topic becomes the **single source of truth** for
  `online`/`offline` and the legacy `last_seen` heartbeat-timeout decay is
  removed (XIU-112). See the section below.

## Presence — `lwt` single source of truth (v0.6, XIU-112)

`EdgeNode.status` (`online` / `offline` / `pending`) is derived **solely**
from the broker's retained `edge/<id>/lwt` topic:

- On every successful connect the edge publishes `edge/<id>/lwt` with
  `retained=true`, payload `{"state": "online", "ts": ...}`.
- The edge also re-publishes that retained `online` on a fixed **presence
  keepalive** cadence (`EDGE_MQTT_PRESENCE_INTERVAL_S`, default 20 s — v0.6
  / XIU-129), independent of any uplink traffic. This is what lets an
  *idle* edge recover after a broker restart: a `persistence false` broker
  drops all retained payloads on bounce, and an idle edge publishes nothing
  that would notice the dead aiomqtt session. The keepalive doubles as the
  liveness probe — a failed re-publish tears the dead session down and
  reconnects, which re-announces `online`. Without it, an idle edge that
  lived through a broker restart stayed wedged `offline` forever (pure-LWT
  center has no other recovery path). Active edges already self-healed via
  their next uplink publish; the keepalive closes the idle gap.
- The edge registers an MQTT **last-will** on the same topic with payload
  `{"state": "offline", "ts": ...}`. The broker auto-publishes it on any
  ungraceful disconnect (TCP RST, edge crash, keepalive timeout).
- The center subscribes `edge/+/lwt` and folds each message into
  `EdgeNode.status` in `fleet.presence.apply_lwt`: `online → ONLINE`
  (also refreshes `last_seen`), `offline → OFFLINE` (also writes one
  synthesised `session.offline` lifecycle row).

**No time-based decay.** Before v0.6 the center additionally swept any
`ONLINE` edge whose `last_seen` was older than `OFFLINE_AFTER` (30 s) back
to `OFFLINE` — inherited from the Phase 1 WS heartbeat model. That was
**removed**: an idle edge (no acquisition task dispatched) legitimately
sends no uplink and never refreshes `last_seen`, yet stays connected via
MQTT keepalive with its retained lwt still `online`. The old decay
therefore mis-flagged healthy idle edges offline (operator alarm noise,
flaky dual-edge acceptance, and a 60 s Playwright `fleet.spec.ts`
preflight timeout). The broker's keepalive + last-will already detect a
genuinely dead edge and publish `offline` within the keepalive window.

`last_seen` is retained as a **diagnostic** field only (last observed
uplink/lwt timestamp); it no longer participates in the status verdict.
`EdgeNode.is_stale()` now reports `status not in {online, pending}` with no
time dependency. Center implementation: `fleet/presence.py` (write path),
`fleet/models.py::EdgeNode.is_stale`, `fleet/views.py` (list no longer
sweeps); the `fleet.services.sweep_stale_edges` helper was deleted.

| frame            | direction        | version | purpose                                       |
|------------------|------------------|---------|-----------------------------------------------|
| `register`       | edge → center    | v0.1    | First frame; `edge_id` + `token` + `version`. |
| `heartbeat`      | edge → center    | v0.1    | ~1 Hz liveness; updates `last_seen`.          |
| `ack`            | center → edge    | v0.1    | Acks a prior edge → center frame.             |
| `error`          | center → edge    | v0.1    | Fatal protocol/auth error; center closes.     |
| `apply_config`   | center → edge    | v0.2    | Full task/device/point snapshot; v0.4 also carries `alarm_rules`. |
| `config_applied` | edge → center    | v0.2    | Result of applying a snapshot.                |
| `task_state`     | edge → center    | v0.2    | Per-task lifecycle transition (no seq).       |

`task_state` is **retained** so a v0.2 edge talking to a v0.3 center keeps
working. A v0.3 edge no longer emits `task_state`; it emits the richer
`lifecycle` frame instead (see below). The center accepts both.

See this file's git history for the full v0.1/v0.2 field schemas of the
frames above.

## Uplink sequencing — `monotonic_seq`

v0.3 introduces a single per-edge **uplink sequence number**. Every
`lifecycle`, `sample_batch` and (v0.4) `alarm_event` frame carries
`monotonic_seq`: a strictly increasing integer (`>= 1`) assigned by the
edge-agent at enqueue time, shared across all uplink frame types so they
form one ordered stream.

Edge side (**v0.5 — persistent counter**):

- The counter lives in the edge's durable SQLite outbox
  (`edge_uplink_meta.seq_high`). It increments by 1 per uplink frame and
  **survives a process restart** — a restarted edge resumes at `N+1`, not
  `1`. This is the change from v0.3/v0.4's per-process counter and is what
  makes restart-safe backfill possible.
- Every uplink frame is persisted to the outbox **before** it hits the
  wire, so a crash between persist and send cannot lose it.

Center side (**v0.5 — no register reset**):

- The center stores the highest value it has accepted per edge as
  `EdgeNode.last_uplink_seq`. v0.5 **no longer resets it on `register`** —
  the edge counter is persistent, so the high-water mark stays valid
  across WS sessions. The center returns it in the `register` ack
  (`last_uplink_seq`) so the reconnecting edge knows its backfill start.
- Within an established stream (`last_uplink_seq > 0`):
  - `monotonic_seq <= last_uplink_seq` is a **duplicate** / replay: the
    center applies it idempotently and does **not** move the high-water
    mark backward. This is what makes backfill safe — a re-sent frame the
    center already had is dropped, never doubled.
  - `monotonic_seq == last_uplink_seq + 1` **advances** the stream.
  - `monotonic_seq > last_uplink_seq + 1` is a **gap**: the center logs
    it, still records the frame, and advances the mark. In normal M5
    operation the edge backfills proactively so the center never sees a
    gap; a gap verdict means the edge's durable outbox overflowed its cap.
- `last_uplink_seq == 0` means no frame ever accepted (a brand-new edge);
  its first frame is `advanced`.
- A frame with `monotonic_seq <= 0` is malformed and rejected with a
  `bad_frame` error.

`register` / `heartbeat` / `config_applied` / `task_state` do **not**
carry `monotonic_seq` — they are control frames, not part of the uplink
data stream.

## Offline degradation + reconnect backfill (v0.5 — M5)

When the center is unreachable the edge keeps acquiring and keeps
evaluating alarms locally; uplink frames accumulate in the **durable
SQLite outbox** instead of an in-process queue, so they survive an
edge-agent restart mid-outage.

On reconnect:

1. The edge `register`s, advertising its `uplink_seq` (persistent
   high-water). The center replies `ack` with its own `last_uplink_seq`.
2. The edge prunes outbox frames `<= last_uplink_seq` (already delivered)
   and **backfills** the rest in `monotonic_seq` order, tagged
   `backfill: true`. Backfill is sent in batches with a pause between
   them so a large backlog cannot flood the center on reconnect.
3. The center processes backfilled frames through the **same**
   `classify_uplink_seq` path — duplicates are dropped idempotently, so
   backfill produces **zero loss and zero duplicate** rows.
4. The center acks each accepted frame with the new `last_uplink_seq`;
   the edge prunes its outbox up to that point, keeping the table small.

If the edge's durable buffer was wiped (its `uplink_seq` is behind the
center), `sync_to_center` fast-forwards the edge counter to the center's
mark so the stream stays monotonic.

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

`EdgeLifecycleEvent` is append-only, so a flapping edge would grow it
without bound. A daily Celery beat task (`fleet.tasks.cleanup_edge_lifecycle_events`)
prunes rows older than `EDGE_LIFECYCLE_EVENT_RETENTION_DAYS` (default 7,
matching the `edge_sample` InfluxDB mirror). Set it to `0` to disable
pruning. The prune deletes in chunks off the ingest path, so it never
stalls live uplink handling.

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

## `apply_config` — `alarm_rules` (v0.4)

v0.4 adds one **optional** array field, `alarm_rules`, to the existing
`apply_config` frame. It carries every threshold rule the edge evaluates
locally. A v0.4 center always includes it (possibly empty `[]`); a v0.3
edge ignores the unknown field.

```json
{
  "v": "0.4",
  "type": "apply_config",
  "version": 7,
  "tasks": [ ... ], "devices": [ ... ], "points": [ ... ],
  "alarm_rules": [
    {"id": 3, "name": "holding_0 过高", "point_code": "holding_0",
     "device_code": "", "operator": "gt", "threshold": 100.0,
     "threshold_high": null, "severity": "warning",
     "is_active": true, "description": ""}
  ]
}
```

Each `alarm_rules[*]` object mirrors one center-side `AlarmRule` row:

| field            | type            | notes                                                       |
|------------------|-----------------|--------------------------------------------------------------|
| `id`             | integer         | Center `AlarmRule.pk`; the edge mirrors the rule under it.  |
| `name`           | string          | Human-readable rule name.                                   |
| `point_code`     | string          | Reading `code` the rule matches.                            |
| `device_code`    | string          | Empty = "any device with this point".                      |
| `operator`       | string          | `gt`/`ge`/`lt`/`le`/`eq`/`ne`/`between`/`outside`.          |
| `threshold`      | number / null   | Primary threshold.                                          |
| `threshold_high` | number / null   | Upper bound for `between` / `outside`.                      |
| `severity`       | string          | `info` / `warning` / `critical`.                            |
| `is_active`      | bool            | Center sends only active rules; field kept for clarity.     |
| `description`    | string          | Optional free text.                                         |

Rule definitions are authored and stored **only at the center**. The
center sends the full active set on every `apply_config` and on every
rule create / update / delete (the center re-pushes a fresh snapshot to
each online edge). The edge mirrors the snapshot into its local SQLite
under the same primary keys and **culls** any rule no longer present, so
a deactivated or deleted rule stops firing on the edge. The edge's
in-process `AlarmSink` evaluates these rules against live readings
exactly as a monolith deployment does — the evaluation code
(`acquisition/services/alarms.py`) is shared, not duplicated.

## `alarm_event` — edge → center  (v0.4)

Reports one locally-triggered alarm. Emitted by the edge-agent on every
fire transition its local `AlarmSink` detects.

```json
{
  "v": "0.4",
  "type": "alarm_event",
  "edge_id": "edge-shanghai-line-1",
  "monotonic_seq": 12,
  "rule_id": 3,
  "point_code": "holding_0",
  "device_code": "plc-1",
  "value": 137.0,
  "severity": "warning",
  "status": "firing",
  "message": "holding_0=137.0 触发规则 [holding_0 过高] > 100.0",
  "fired_at": "2026-05-22T03:14:09.880Z"
}
```

| field           | type          | required | notes                                                       |
|-----------------|---------------|----------|--------------------------------------------------------------|
| `monotonic_seq` | integer       | yes      | Per-edge uplink sequence (see above).                        |
| `rule_id`       | integer       | yes      | Center `AlarmRule.pk` the alarm belongs to.                  |
| `point_code`    | string        | yes      | Reading code that breached the threshold.                    |
| `device_code`   | string        | no       | Device the point belongs to; `""` if rule is device-agnostic.|
| `value`         | number/bool/str | yes    | The breaching value (engineering units).                     |
| `severity`      | string        | no       | `info` / `warning` / `critical`; mirrors the rule.           |
| `status`        | string        | no       | `firing` (default) or `cleared` — both emitted since v0.5.   |
| `message`       | string        | no       | Human-readable alarm text.                                   |
| `fired_at`      | string        | no       | Edge wall-clock ISO-8601 UTC; informational.                 |

On receipt the center classifies `monotonic_seq` exactly like a
`lifecycle` frame (duplicate → drop, advance / gap → record). For a
non-duplicate `firing` event it creates an `acquisition.Alarm` row with
the `edge` FK set to the reporting edge — that is what lets the center
`/alarms` page show **which edge** raised each alarm. The center
deduplicates idempotently: one open `firing` `Alarm` per
`(rule, edge, point_code, device_code)`, so a redelivered frame or an
edge restart re-firing the same condition does not pile up rows. An
`alarm_event` whose `rule_id` no longer exists center-side is dropped,
but the uplink sequence still advances.

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
| `alarm_event`    | `ack` with `ref="alarm_event"`         |

v0.5: every `ack` carries `last_uplink_seq` — the center's high-water
mark for this edge. The edge uses it to prune its durable outbox up to the
confirmed seq; on the `register` ack it is also the backfill start point.

## Reconnect behaviour (client side)

- On any close that isn't `1000`/`1001`, the edge waits an
  exponential-backoff delay (1 → 2 → 4 → 8 → 16 → 30 s, capped at 30 s)
  and reconnects.
- The backoff timer is **reset to 1 s** after the next successful
  `register` ack.
- On reconnect the center re-pushes the latest `apply_config`.
- v0.5: uplink frames produced while the socket is down accumulate in the
  edge's **durable SQLite outbox** — they survive an edge-agent restart
  mid-outage and are backfilled in `monotonic_seq` order on reconnect
  (see *Offline degradation + reconnect backfill* above).

## Out of scope for v0.5 (planned for M6+)

- Historical query proxy — M6
- mTLS / signed bootstrap, beyond the bare activation token — M7
