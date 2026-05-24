"""M6 history proxy — center → edge ``GET /history/points`` (XIU-83).

Single-machine ``/data`` historically queried the center's InfluxDB
directly. In fleet mode raw samples only live on the edge that produced
them; this module is the thin center-side proxy that resolves
``task_id → AcqTask.edge`` and forwards the query to the edge's M6
read-only HTTP server.

See ``docs/distributed/history-proxy.md`` for the wire contract.
"""
