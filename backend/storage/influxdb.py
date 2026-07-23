"""InfluxDB storage backend for time-series data.

Throughput & reliability design (issue XIU-3):

* **C2 — batched writes.** The live write path uses an *asynchronous batching*
  ``write_api`` (``WriteType.batching``). ``write()`` only enqueues points and
  returns immediately; the client coalesces them into HTTP requests of up to
  ``batch_size`` points flushed every ``flush_interval`` ms. This replaces the
  old ``SYNCHRONOUS`` api whose every ``write()`` blocked on a round-trip.

* **M4 — failure backoff & closed loop.** Batching write failures are reported
  asynchronously via ``error_callback``; the failed line-protocol batch is
  spilled to a durable :class:`~storage.spill_queue.SpillQueue` (SQLite) that
  survives process restarts. A background replay worker drains the queue back
  into InfluxDB — using a *synchronous* api so success is confirmed before a
  row is deleted. ``is_available()`` reflects a circuit-breaker fed by the
  success/error callbacks, so the failure path is fully closed.

* **M5 — cardinality** is handled by the producer (``InfluxDBSink``): only
  low-cardinality tags reach this layer.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from influxdb_client import InfluxDBClient as InfluxClient
from influxdb_client.client.write_api import SYNCHRONOUS, WriteOptions, WriteType

from .base import BaseStorage, StorageError, StorageRegistry, WriteError
from .spill_queue import SpillQueue


@StorageRegistry.register("influxdb")
class InfluxDBStorage(BaseStorage):
    """
    InfluxDB 2.x storage implementation.

    Stores time-series acquisition data with tags and fields.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.url = config.get("url") or f"http://{config.get('host', 'localhost')}:{config.get('port', 8086)}"
        self.token = config.get("token")
        self.org = config.get("org", "default")
        self.bucket = config.get("bucket", "default")
        self.docker_mode = config.get("docker_mode", False)  # Use docker exec for writing
        self.container_name = config.get("container_name", "influxdb")

        # --- C2: batching write tuning (overridable via config) -------------
        self.batch_size = int(config.get("batch_size", 500))
        self.flush_interval = int(config.get("flush_interval", 1000))   # ms
        self.jitter_interval = int(config.get("jitter_interval", 200))  # ms

        # --- retry tuning (overridable via config; same defaults as before,
        # this was previously hardcoded in connect()). Found under chaos
        # testing: with the defaults, a single failing batch keeps the
        # batching write_api's internal (non-daemon!) retry threads busy for
        # 5s + 10s + 20s ~= tens of seconds before giving up and spilling —
        # and that is per batch, serialized, for however many batches are
        # in flight. Tests that point at a deliberately unreachable backend
        # should shrink these so they don't hang for a long time and don't
        # leave long-lived non-daemon retry threads behind at interpreter
        # exit (they are NOT daemonized by the underlying reactivex
        # ThreadPoolExecutor, so a process can fail to exit cleanly while
        # they are still retrying — see the report for detail).
        self.write_retry_interval = int(config.get("write_retry_interval", 5_000))    # ms
        self.write_max_retries = int(config.get("write_max_retries", 3))
        self.write_max_retry_delay = int(config.get("write_max_retry_delay", 30_000))  # ms
        self.write_exponential_base = int(config.get("write_exponential_base", 2))

        # --- M4: spill queue + replay tuning --------------------------------
        # The spill DB lives next to the Django db by default so it shares the
        # instance's data directory and survives restarts.
        spill_path = config.get("spill_db_path") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), os.pardir, "influx_spill.sqlite3"
        )
        self.spill_db_path = os.path.abspath(spill_path)
        self.replay_interval = float(config.get("replay_interval", 5.0))  # seconds
        # Circuit breaker: after this many consecutive failures is_available()
        # reports unavailable until a cooldown elapses (then a half-open probe).
        self.circuit_failure_threshold = int(config.get("circuit_failure_threshold", 5))
        self.circuit_cooldown = float(config.get("circuit_cooldown", 30.0))

        self.client = None
        self.write_api = None      # asynchronous batching api (live writes)
        self.replay_api = None     # synchronous api (deterministic spill replay)

        # Circuit breaker / health tracking. Mutated from callback threads.
        self._consecutive_failures = 0
        self._last_success_time = 0.0
        self._last_failure_time = 0.0

        # Durable spill queue is created eagerly so failed batches can be
        # persisted even if a later connect() attempt fails.
        try:
            self._spill: Optional[SpillQueue] = SpillQueue(self.spill_db_path)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Failed to open spill queue at %s: %s", self.spill_db_path, exc)
            self._spill = None

        self._replay_thread: Optional[threading.Thread] = None
        self._replay_stop = threading.Event()

    def connect(self) -> bool:
        """Connect to InfluxDB."""
        try:
            self.client = InfluxClient(
                url=self.url,
                token=self.token,
                org=self.org
            )
            # C2: asynchronous batching write api. write() now only enqueues.
            write_options = WriteOptions(
                write_type=WriteType.batching,
                batch_size=self.batch_size,
                flush_interval=self.flush_interval,
                jitter_interval=self.jitter_interval,
                retry_interval=self.write_retry_interval,
                max_retries=self.write_max_retries,
                max_retry_delay=self.write_max_retry_delay,
                exponential_base=self.write_exponential_base,
            )
            self.write_api = self.client.write_api(
                write_options=write_options,
                success_callback=self._on_write_success,
                error_callback=self._on_write_error,
                retry_callback=self._on_write_retry,
            )
            # Separate SYNCHRONOUS api: spill replay must know a write
            # actually landed before deleting the row from the durable queue.
            self.replay_api = self.client.write_api(write_options=SYNCHRONOUS)
            self.is_connected = True
            # Reset failure counter on successful connection
            self._consecutive_failures = 0
            self._start_replay_worker()
            self.logger.info(
                "Connected to InfluxDB at %s (batch_size=%d, flush_interval=%dms)",
                self.url, self.batch_size, self.flush_interval,
            )
            return True
        except Exception as e:
            self.is_connected = False
            self.logger.error(f"Failed to connect to InfluxDB: {e}")
            raise StorageError(f"InfluxDB connection failed: {e}") from e

    def disconnect(self) -> None:
        """Close InfluxDB connection."""
        # Stop the replay worker first so it does not touch a closing client.
        self._replay_stop.set()
        if self._replay_thread and self._replay_thread.is_alive():
            self._replay_thread.join(timeout=5.0)
        self._replay_thread = None

        if self.write_api:
            try:
                # close() flushes any batched-but-unsent points; a failure
                # here surfaces through error_callback -> spill queue.
                self.write_api.close()
            except Exception as e:
                self.logger.warning(f"Error closing write API: {e}")
            finally:
                self.write_api = None

        if self.replay_api:
            try:
                self.replay_api.close()
            except Exception as e:
                self.logger.warning(f"Error closing replay API: {e}")
            finally:
                self.replay_api = None

        if self.client:
            try:
                self.client.close()
                self.is_connected = False
                self.logger.info("Disconnected from InfluxDB")
            except Exception as e:
                self.logger.warning(f"Error during disconnect: {e}")
            finally:
                self.client = None

    def write(self, data: List[Dict[str, Any]]) -> bool:
        """
        Write data points to InfluxDB.

        With the batching write api this call only *enqueues* the points and
        returns immediately — the client flushes them in the background.
        Failures are therefore reported asynchronously through
        :meth:`_on_write_error`, which spills the batch to the durable queue.

        Args:
            data: List of data points with structure:
                {
                    "measurement": str,      # Measurement name
                    "tags": dict,           # Tags (indexed metadata)
                    "fields": dict,         # Fields (actual data)
                    "time": int (optional)  # Timestamp in nanoseconds
                }

        Returns:
            True if the batch was accepted for writing.

        Raises:
            WriteError: If the points could not even be enqueued.
        """
        if not self.is_connected or not self.write_api:
            if not self.connect():
                raise WriteError("Not connected to InfluxDB")

        if not data:
            return True

        # Convert data to InfluxDB line protocol format
        points = self._format_points(data)
        if not points:
            return True

        # Use docker exec if docker_mode is enabled (workaround for WSL2 auth issues)
        if self.docker_mode:
            return self._write_via_docker(points)

        try:
            # Batching api: enqueue only — no network round-trip here.
            self.write_api.write(bucket=self.bucket, org=self.org, record=points)
            return True
        except Exception as e:
            # Reaching here means the client rejected the points outright
            # (closed api, serialization error). Spill so nothing is lost,
            # then surface the error to the caller.
            self.logger.error(f"Failed to enqueue write to InfluxDB: {e}")
            self._spill_points(points)
            raise WriteError(f"InfluxDB write failed: {e}") from e

    # --------------------------------------------------------- write callbacks
    def _on_write_success(self, conf: Any, data: Any) -> None:
        """Batching api success callback — clears the circuit breaker."""
        if self._consecutive_failures:
            self.logger.info(
                "InfluxDB write recovered after %d consecutive failure(s)",
                self._consecutive_failures,
            )
        self._consecutive_failures = 0
        self._last_success_time = time.time()

    def _on_write_error(self, conf: Any, data: Any, exception: Exception) -> None:
        """Batching api error callback — spill the failed batch (M4)."""
        self._consecutive_failures += 1
        self._last_failure_time = time.time()
        line_protocol = data.decode() if isinstance(data, (bytes, bytearray)) else str(data or "")
        self.logger.warning(
            "InfluxDB async write failed (#%d), spilling batch to disk queue: %s",
            self._consecutive_failures, exception,
        )
        if self._spill and line_protocol:
            try:
                self._spill.push(line_protocol, points=self._count_lines(line_protocol))
            except Exception as exc:  # noqa: BLE001
                self.logger.error("Failed to spill failed batch to disk: %s", exc)

    def _on_write_retry(self, conf: Any, data: Any, exception: Exception) -> None:
        """Batching api retry callback — informational only."""
        self.logger.debug("InfluxDB write retry scheduled: %s", exception)

    # ------------------------------------------------------------ spill replay
    def _start_replay_worker(self) -> None:
        """Launch the background thread that drains the spill queue."""
        if self._spill is None:
            return
        if self._replay_thread and self._replay_thread.is_alive():
            return
        self._replay_stop.clear()
        self._replay_thread = threading.Thread(
            target=self._replay_loop,
            daemon=True,
            name="InfluxSpillReplay",
        )
        self._replay_thread.start()

    def _replay_loop(self) -> None:
        while not self._replay_stop.is_set():
            # wait() returns immediately (True) once disconnect() fires.
            if self._replay_stop.wait(self.replay_interval):
                break
            try:
                self._drain_spill_once()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Spill replay iteration failed: %s", exc)

    def _drain_spill_once(self) -> None:
        """Replay spilled batches back into InfluxDB until the queue is empty.

        Uses the SYNCHRONOUS ``replay_api`` so a batch is deleted from the
        durable queue only after the write is confirmed. Re-written points are
        idempotent in InfluxDB (same measurement/tags/field/timestamp), so a
        concurrent replayer or a crash mid-drain causes at most a harmless
        duplicate, never data loss.
        """
        if not self.is_connected or not self.replay_api or self._spill is None:
            return
        # Honour the circuit breaker: while it is open, only probe once the
        # cooldown has elapsed instead of hammering a known-sick backend.
        if self._consecutive_failures >= self.circuit_failure_threshold:
            if (time.time() - self._last_failure_time) < self.circuit_cooldown:
                return

        replayed = 0
        while not self._replay_stop.is_set():
            rows = self._spill.peek_batch(self.batch_size)
            if not rows:
                break
            payload = "\n".join(r[1] for r in rows)
            try:
                self.replay_api.write(bucket=self.bucket, org=self.org, record=payload)
            except Exception as exc:  # noqa: BLE001
                self._consecutive_failures += 1
                self._last_failure_time = time.time()
                self.logger.warning(
                    "Spill replay write failed (%d batch(es) retained): %s",
                    len(rows), exc,
                )
                return
            self._spill.delete([r[0] for r in rows])
            self._consecutive_failures = 0
            self._last_success_time = time.time()
            replayed += len(rows)

        if replayed:
            self.logger.info(
                "Spill replay: re-wrote %d batch(es) to InfluxDB (%d remaining)",
                replayed, self._spill.count(),
            )

    def _spill_points(self, points: List[Dict[str, Any]]) -> None:
        """Persist already-formatted points to the durable spill queue."""
        if self._spill is None or not points:
            return
        try:
            line_protocol = self._points_to_line_protocol(points)
            if line_protocol:
                self._spill.push(line_protocol, points=len(points))
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Failed to spill points to disk: %s", exc)

    @staticmethod
    def _count_lines(line_protocol: str) -> int:
        if not line_protocol:
            return 0
        return line_protocol.count("\n") + 1

    @property
    def spill_pending(self) -> int:
        """Number of batches currently awaiting replay (0 when healthy)."""
        return self._spill.count() if self._spill else 0

    def health_check(self) -> bool:
        """Check InfluxDB health."""
        if not self.is_connected or not self.client:
            return False
        try:
            # Try to ping the InfluxDB instance
            health = self.client.health()
            return health.status == "pass"
        except Exception as e:
            self.logger.warning(f"Health check failed: {e}")
            return False

    def is_available(self) -> bool:
        """
        Quick check if storage is available for writing.

        Closes the failure loop (M4): the circuit opens after
        ``circuit_failure_threshold`` consecutive failures and stays open for
        ``circuit_cooldown`` seconds, after which a half-open probe is allowed.

        Returns:
            True if storage is connected and the circuit breaker is closed.
        """
        if not self.is_connected:
            return False
        if self._consecutive_failures >= self.circuit_failure_threshold:
            # Circuit open — allow a half-open probe once the cooldown passes.
            return (time.time() - self._last_failure_time) >= self.circuit_cooldown
        return True

    def _format_points(self, data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Format data points for InfluxDB.

        Args:
            data: Raw data points

        Returns:
            Formatted points ready for InfluxDB
        """
        formatted = []

        for point in data:
            if not point.get("measurement") or not point.get("fields"):
                self.logger.warning(f"Skipping invalid point: {point}")
                continue

            # Filter out unsupported field types (lists, dicts, etc.)
            # InfluxDB only supports: int, float, str, bool
            clean_fields = {}
            for key, val in point.get("fields", {}).items():
                if isinstance(val, (int, float, str, bool)):
                    clean_fields[key] = val
                elif isinstance(val, (list, dict)):
                    # Convert complex types to JSON string
                    import json
                    clean_fields[key] = json.dumps(val)
                    self.logger.debug(f"Converted {key} from {type(val).__name__} to JSON string")
                else:
                    # Try to convert to string
                    clean_fields[key] = str(val)

            if not clean_fields:
                self.logger.warning(f"No valid fields after filtering for point: {point}")
                continue

            formatted_point = {
                "measurement": point["measurement"],
                "tags": point.get("tags", {}),
                "fields": clean_fields,
            }

            # Add timestamp if provided, otherwise InfluxDB will use current time
            if "time" in point and point["time"]:
                formatted_point["time"] = int(point["time"])
            elif "timestamp" in point and point["timestamp"]:
                formatted_point["time"] = int(point["timestamp"])

            formatted.append(formatted_point)

        return formatted

    def query(self, flux_query: str) -> List[Dict[str, Any]]:
        """
        Execute Flux query against InfluxDB.

        Args:
            flux_query: Flux query string

        Returns:
            List of query results

        Raises:
            StorageError: If query fails
        """
        if not self.is_connected or not self.client:
            if not self.connect():
                raise StorageError("Not connected to InfluxDB")

        try:
            query_api = self.client.query_api()
            result = query_api.query(query=flux_query, org=self.org)

            # Convert result to list of dicts
            records = []
            for table in result:
                for record in table.records:
                    records.append(record.values)

            return records

        except Exception as e:
            self.logger.error(f"Query failed: {e}")
            raise StorageError(f"InfluxDB query failed: {e}") from e

    def get_point_count(
        self,
        measurement: str,
        field: str,
        start_time: str,
        stop_time: str
    ) -> int:
        """
        Get count of data points for a specific field.

        Args:
            measurement: Measurement name
            field: Field name
            start_time: Start time (RFC3339 or relative)
            stop_time: Stop time (RFC3339 or relative)

        Returns:
            Count of matching points
        """
        flux_query = f'''
        from(bucket:"{self.bucket}")
          |> range(start: {start_time}, stop: {stop_time})
          |> filter(fn: (r) => r["_measurement"] == "{measurement}")
          |> filter(fn: (r) => r["_field"] == "{field}")
          |> count(column: "_value")
          |> yield(name: "count")
        '''

        try:
            result = self.query(flux_query)
            if result:
                return result[0].get("_value", 0)
            return 0
        except Exception as e:
            self.logger.error(f"Failed to get point count: {e}")
            return 0

    def _points_to_line_protocol(self, points: List[Dict[str, Any]]) -> str:
        """Serialize formatted points to InfluxDB line protocol.

        Shared by the docker-exec write path and the spill queue so both
        produce identical, replayable line protocol.
        """
        # Escape special characters in tag keys/values (space, comma, equals)
        def escape_tag(v: Any) -> str:
            return str(v).replace(' ', '\\ ').replace(',', '\\,').replace('=', '\\=')

        lines: List[str] = []
        for point in points:
            measurement = str(point.get("measurement", "")).replace(' ', '\\ ').replace(',', '\\,')
            tags_dict = point.get("tags", {}) or {}
            fields_dict = point.get("fields", {}) or {}
            timestamp = point.get("time")

            tags_str = ",".join(
                f"{escape_tag(k)}={escape_tag(v)}" for k, v in tags_dict.items()
            )
            fields_str = ",".join([
                f"{k}={v}i" if isinstance(v, int) and not isinstance(v, bool)
                else f"{k}={str(v).lower()}" if isinstance(v, bool)
                else f"{k}={v}" if isinstance(v, (int, float))
                else f'{k}="{v}"'
                for k, v in fields_dict.items()
            ])
            if not fields_str:
                continue

            if tags_str:
                line = f"{measurement},{tags_str} {fields_str}"
            else:
                line = f"{measurement} {fields_str}"

            # Add timestamp if provided (must be integer nanoseconds)
            if timestamp:
                try:
                    ts_int = int(timestamp)
                    # Valid range: 2020-01-01 .. 2100-01-01 in nanoseconds.
                    if 1577836800000000000 <= ts_int <= 4102444800000000000:
                        line += f" {ts_int}"
                    else:
                        self.logger.warning(f"Timestamp {ts_int} out of valid range, using current time")
                        line += f" {int(time.time() * 1e9)}"
                except (ValueError, TypeError):
                    self.logger.warning(f"Invalid timestamp {timestamp}, using current time")
                    line += f" {int(time.time() * 1e9)}"

            lines.append(line)

        return "\n".join(lines)

    def _write_via_docker(self, points: List[Dict[str, Any]]) -> bool:
        """
        Write to InfluxDB using docker exec (workaround for WSL2 port mapping auth issues).

        Args:
            points: Formatted points ready for InfluxDB

        Returns:
            True if write successful

        Raises:
            WriteError: If write fails
        """
        try:
            line_protocol = self._points_to_line_protocol(points)
            if not line_protocol:
                return True

            # Execute docker command using stdin to handle multi-line data properly
            cmd = [
                "docker", "exec", "-i", self.container_name,
                "influx", "write",
                "-b", self.bucket,
                "-o", self.org,
                "-t", self.token
            ]

            result = subprocess.run(cmd, input=line_protocol, capture_output=True, text=True, timeout=10)

            if result.returncode == 0:
                self.logger.debug(f"Successfully wrote {len(points)} points via docker exec")
                self._consecutive_failures = 0
                self._last_success_time = time.time()
                return True
            else:
                self._consecutive_failures += 1
                self._last_failure_time = time.time()
                error_msg = result.stderr or result.stdout
                self.logger.error(f"Docker exec write failed (attempt {self._consecutive_failures}): {error_msg}")
                # M4: persist the failed batch so it is replayed on recovery.
                if self._spill:
                    self._spill.push(line_protocol, points=len(points))
                raise WriteError(f"Docker exec write failed: {error_msg}")

        except WriteError:
            raise
        except subprocess.TimeoutExpired:
            self._consecutive_failures += 1
            self._last_failure_time = time.time()
            self.logger.error("Docker exec write timed out")
            self._spill_points(points)
            raise WriteError("Docker exec write timed out")
        except Exception as e:
            self._consecutive_failures += 1
            self._last_failure_time = time.time()
            self.logger.error(f"Docker exec write error: {e}")
            self._spill_points(points)
            raise WriteError(f"Docker exec write error: {e}") from e
