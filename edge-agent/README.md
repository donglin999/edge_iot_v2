# edge-agent

Distributed edge gateway agent for `edge_iot_v2`.

M1 scope (this milestone):

- Connect to the center over `ws://<center>/ws/fleet/`
- Send `register` once on connect, then `heartbeat` every ~1 s
- Reconnect with exponential backoff (1 → 2 → 4 → 8 → 16 → 30 s, capped)
- **No** actual acquisition tasks run yet — pure control-plane

## Configuration

| env             | required | example                       |
|-----------------|----------|-------------------------------|
| `EDGE_ID`       | yes      | `edge-line-1`                 |
| `EDGE_TOKEN`    | yes      | (from `POST /api/fleet/edges/`) |
| `CENTER_URL`    | yes      | `ws://center.local:8000/ws/fleet/` |
| `EDGE_LABELS`   | no       | JSON object, e.g. `{"site":"shanghai"}` |
| `LOG_LEVEL`     | no       | `INFO` (default), `DEBUG`     |

## Run locally

```bash
cd edge-agent
pip install -e .
EDGE_ID=edge-1 EDGE_TOKEN=... CENTER_URL=ws://localhost:8000/ws/fleet/ edge-agent
```

See `docs/distributed/protocol.md` for the wire format.
