"""InfluxDB storage backend for time-series data."""
from __future__ import annotations

import subprocess
import time
from typing import Any, Dict, List

from influxdb_client import InfluxDBClient as InfluxClient
from influxdb_client.client.write_api import SYNCHRONOUS

from .base import BaseStorage, StorageError, StorageRegistry, WriteError


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
        self.client = None
        self.write_api = None

    def connect(self) -> bool:
        """Connect to InfluxDB."""
        try:
            self.client = InfluxClient(
                url=self.url,
                token=self.token,
                org=self.org
            )
            self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
            self.is_connected = True
            self.logger.info(f"Connected to InfluxDB at {self.url}")
            return True
        except Exception as e:
            self.is_connected = False
            self.logger.error(f"Failed to connect to InfluxDB: {e}")
            raise StorageError(f"InfluxDB connection failed: {e}") from e

    def disconnect(self) -> None:
        """Close InfluxDB connection."""
        if self.write_api:
            try:
                self.write_api.close()
            except Exception as e:
                self.logger.warning(f"Error closing write API: {e}")
            finally:
                self.write_api = None

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

        Args:
            data: List of data points with structure:
                {
                    "measurement": str,      # Measurement name
                    "tags": dict,           # Tags (indexed metadata)
                    "fields": dict,         # Fields (actual data)
                    "time": int (optional)  # Timestamp in nanoseconds
                }

        Returns:
            True if write successful.

        Raises:
            WriteError: If write fails.
        """
        if not self.is_connected or not self.write_api:
            if not self.connect():
                raise WriteError("Not connected to InfluxDB")

        if not data:
            return True

        try:
            # Convert data to InfluxDB line protocol format
            points = self._format_points(data)

            # Use docker exec if docker_mode is enabled (workaround for WSL2 auth issues)
            if self.docker_mode:
                return self._write_via_docker(points)

            # Write to InfluxDB
            self.write_api.write(bucket=self.bucket, org=self.org, record=points)

            self.logger.debug(f"Successfully wrote {len(points)} points to InfluxDB")
            return True

        except Exception as e:
            self.logger.error(f"Failed to write to InfluxDB: {e}")
            # Try docker mode as fallback
            if not self.docker_mode:
                self.logger.info("Attempting docker exec fallback...")
                try:
                    points = self._format_points(data)
                    return self._write_via_docker(points)
                except Exception as docker_err:
                    self.logger.error(f"Docker fallback also failed: {docker_err}")
            raise WriteError(f"InfluxDB write failed: {e}") from e

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
            # Convert points to line protocol
            lines = []
            for point in points:
                measurement = point.get("measurement")
                tags_dict = point.get("tags", {})
                fields_dict = point.get("fields", {})
                timestamp = point.get("time")

                # Build line protocol: measurement,tag=value field=value timestamp
                # Escape special characters in tag values (space, comma, equals)
                def escape_tag_value(v):
                    return str(v).replace(' ', '\\ ').replace(',', '\\,').replace('=', '\\=')

                tags_str = ",".join([f"{k}={escape_tag_value(v)}" for k, v in tags_dict.items()]) if tags_dict else ""
                fields_str = ",".join([
                    f"{k}={v}i" if isinstance(v, int) and not isinstance(v, bool)
                    else f"{k}={str(v).lower()}" if isinstance(v, bool)
                    else f"{k}={v}" if isinstance(v, (int, float))
                    else f'{k}="{v}"'
                    for k, v in fields_dict.items()
                ])

                if tags_str:
                    line = f"{measurement},{tags_str} {fields_str}"
                else:
                    line = f"{measurement} {fields_str}"

                # Add timestamp if provided (must be integer nanoseconds)
                if timestamp:
                    try:
                        # Ensure timestamp is an integer
                        ts_int = int(timestamp)
                        # Check if timestamp is reasonable (between 2020 and 2100 in nanoseconds)
                        # Valid range: 1577836800000000000 (2020-01-01) to 4102444800000000000 (2100-01-01)
                        if 1577836800000000000 <= ts_int <= 4102444800000000000:
                            line += f" {ts_int}"
                        else:
                            # If timestamp is out of range, use current time
                            self.logger.warning(f"Timestamp {ts_int} out of valid range, using current time")
                            line += f" {int(time.time() * 1e9)}"
                    except (ValueError, TypeError):
                        # If timestamp conversion fails, use current time
                        self.logger.warning(f"Invalid timestamp {timestamp}, using current time")
                        line += f" {int(time.time() * 1e9)}"

                lines.append(line)

            line_protocol = "\n".join(lines)

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
                return True
            else:
                error_msg = result.stderr or result.stdout
                self.logger.error(f"Docker exec write failed: {error_msg}")
                raise WriteError(f"Docker exec write failed: {error_msg}")

        except subprocess.TimeoutExpired:
            self.logger.error("Docker exec write timed out")
            raise WriteError("Docker exec write timed out")
        except Exception as e:
            self.logger.error(f"Docker exec write error: {e}")
            raise WriteError(f"Docker exec write error: {e}") from e
