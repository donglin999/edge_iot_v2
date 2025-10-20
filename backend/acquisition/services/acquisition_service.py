"""Core acquisition service for data collection orchestration."""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any, Dict, List

from django.conf import settings

from acquisition import models as acq_models
from acquisition.protocols import ProtocolRegistry
from configuration import models as config_models
from storage import StorageRegistry

logger = logging.getLogger(__name__)


class AcquisitionService:
    """
    Orchestrates data acquisition from devices and storage.

    Manages protocol connections, data reading, and storage writes.
    """

    def __init__(
        self,
        task: config_models.AcqTask,
        session: acq_models.AcquisitionSession
    ) -> None:
        """
        Initialize acquisition service.

        Args:
            task: Acquisition task configuration
            session: Active session tracking
        """
        self.task = task
        self.session = session
        self.logger = logging.getLogger(f"{__name__}.{task.code}")

        # Initialize storage backends
        self.storages = self._init_storages()

        # Group points by device for efficient reading
        self.device_groups = self._group_points_by_device()

    def _init_storages(self) -> Dict[str, Any]:
        """Initialize configured storage backends."""
        storages = {}

        # InfluxDB storage - using HTTP API mode for remote server
        influx_config = {
            "url": getattr(settings, "INFLUXDB_URL", None),  # Full URL if provided
            "host": getattr(settings, "INFLUXDB_HOST", "localhost"),
            "port": getattr(settings, "INFLUXDB_PORT", 8086),
            "token": getattr(settings, "INFLUXDB_TOKEN", ""),
            "org": getattr(settings, "INFLUXDB_ORG", "default"),
            "bucket": getattr(settings, "INFLUXDB_BUCKET", "default"),
            "docker_mode": False,  # Use HTTP API for remote InfluxDB
        }
        try:
            influx = StorageRegistry.create("influxdb", influx_config)
            influx.connect()
            storages["influxdb"] = influx
            self.logger.info("InfluxDB storage initialized")
        except Exception as e:
            self.logger.warning(f"Failed to initialize InfluxDB: {e}")

        return storages

    def _group_points_by_device(self) -> Dict[int, Dict[str, Any]]:
        """
        Group points by device for batch reading.

        Returns:
            Dict mapping device_id to device info and points
        """
        groups = defaultdict(lambda: {"device": None, "points": [], "protocol": None})

        for point in self.task.points.select_related("device", "template").all():
            device_id = point.device.id
            if groups[device_id]["device"] is None:
                groups[device_id]["device"] = point.device

            # Convert point to protocol-readable format
            point_config = {
                "code": point.code,
                "address": point.address,
                "type": point.extra.get("type", "int16") if point.extra else "int16",
                "num": point.extra.get("num", 1) if point.extra else 1,
                "coefficient": float(point.template.coefficient) if point.template else 1.0,
                "precision": int(point.template.precision) if point.template else 2,
            }
            groups[device_id]["points"].append(point_config)

        return dict(groups)

    def acquire_once(self) -> Dict[str, Any]:
        """
        Perform single acquisition cycle.

        Returns:
            Dict with acquisition results
        """
        self.logger.info(f"Starting single acquisition for task {self.task.code}")

        all_data = []
        errors = []

        for device_id, group in self.device_groups.items():
            device = group["device"]
            points = group["points"]

            try:
                # Create protocol instance
                device_config = {
                    "source_ip": device.ip_address,
                    "source_port": device.port,
                    "protocol_type": device.protocol,
                    **(device.metadata or {})
                }

                protocol = ProtocolRegistry.create(device.protocol, device_config)

                # Read data
                with protocol:
                    readings = protocol.read_points(points)
                    all_data.extend(self._format_for_storage(readings, device))

            except Exception as e:
                error_msg = f"Failed to read from device {device.code}: {e}"
                self.logger.error(error_msg)
                errors.append({"device": device.code, "error": str(e)})

        # Write to storage
        if all_data:
            self._write_to_storage(all_data)

        return {
            "status": "completed",
            "points_read": len(all_data),
            "errors": errors,
            "data": all_data,
        }

    def run_continuous(self) -> Dict[str, Any]:
        """
        Run continuous acquisition loop with persistent connections.

        Keeps protocol connections open for efficient data collection.
        Implements batching, timeout detection, and auto-reconnection.

        Returns:
            Dict with execution summary
        """
        self.logger.info(f"Starting continuous acquisition for task {self.task.code}")

        # Update session status
        self.session.status = acq_models.AcquisitionSession.STATUS_RUNNING
        self.session.save(update_fields=["status", "updated_at"])

        total_cycles = 0
        total_points = 0
        errors = []

        # Configuration
        batch_size = getattr(settings, "ACQUISITION_BATCH_SIZE", 50)
        batch_timeout = getattr(settings, "ACQUISITION_BATCH_TIMEOUT", 5.0)  # seconds
        connection_timeout = getattr(settings, "ACQUISITION_CONNECTION_TIMEOUT", 30.0)  # seconds
        max_reconnect_attempts = getattr(settings, "ACQUISITION_MAX_RECONNECT_ATTEMPTS", 3)

        # Initialize persistent protocol connections
        device_protocols = {}
        device_health = {}  # Track last successful read time

        try:
            # Establish all protocol connections upfront
            for device_id, group in self.device_groups.items():
                device = group["device"]
                try:
                    device_config = {
                        "source_ip": device.ip_address,
                        "source_port": device.port,
                        "protocol_type": device.protocol,
                        **(device.metadata or {})
                    }
                    protocol = ProtocolRegistry.create(device.protocol, device_config)
                    protocol.connect()
                    device_protocols[device_id] = protocol
                    device_health[device_id] = {
                        "last_success": time.time(),
                        "consecutive_failures": 0,
                        "status": "healthy"
                    }
                    self.logger.info(f"Connected to device {device.code}")
                except Exception as e:
                    self.logger.error(f"Failed to connect to device {device.code}: {e}")
                    device_health[device_id] = {
                        "last_success": None,
                        "consecutive_failures": 1,
                        "status": "disconnected"
                    }

            # Batch data buffer
            batch_buffer = []
            batch_start_time = time.time()

            # Main acquisition loop
            while self._should_continue():
                cycle_start = time.time()

                # Read from all devices
                for device_id, group in self.device_groups.items():
                    device = group["device"]
                    points = group["points"]
                    protocol = device_protocols.get(device_id)

                    if not protocol:
                        # Try to reconnect
                        if device_health[device_id]["consecutive_failures"] < max_reconnect_attempts:
                            try:
                                device_config = {
                                    "source_ip": device.ip_address,
                                    "source_port": device.port,
                                    "protocol_type": device.protocol,
                                    **(device.metadata or {})
                                }
                                protocol = ProtocolRegistry.create(device.protocol, device_config)
                                protocol.connect()
                                device_protocols[device_id] = protocol
                                device_health[device_id]["status"] = "healthy"
                                self.logger.info(f"Reconnected to device {device.code}")
                            except Exception as e:
                                device_health[device_id]["consecutive_failures"] += 1
                                self.logger.warning(f"Reconnect failed for {device.code}: {e}")
                                continue
                        else:
                            continue

                    try:
                        # Read data without disconnecting
                        readings = protocol.read_points(points)
                        formatted_data = self._format_for_storage(readings, device)
                        batch_buffer.extend(formatted_data)

                        # Update health status
                        device_health[device_id]["last_success"] = time.time()
                        device_health[device_id]["consecutive_failures"] = 0
                        device_health[device_id]["status"] = "healthy"

                    except Exception as e:
                        error_msg = f"Failed to read from device {device.code}: {e}"
                        self.logger.error(error_msg)
                        errors.append({"device": device.code, "error": str(e), "time": time.time()})

                        # Update health status
                        device_health[device_id]["consecutive_failures"] += 1

                        # Check for timeout
                        last_success = device_health[device_id]["last_success"]
                        if last_success and (time.time() - last_success) > connection_timeout:
                            device_health[device_id]["status"] = "timeout"
                            self.logger.warning(f"Device {device.code} timeout detected")

                            # Disconnect and attempt reconnect on next cycle
                            try:
                                protocol.disconnect()
                            except:
                                pass
                            device_protocols[device_id] = None
                        else:
                            device_health[device_id]["status"] = "error"

                # Write batch to storage if buffer is full or timeout reached
                batch_elapsed = time.time() - batch_start_time
                if batch_buffer and (len(batch_buffer) >= batch_size or batch_elapsed >= batch_timeout):
                    self._write_to_storage(batch_buffer)
                    total_points += len(batch_buffer)
                    batch_buffer = []
                    batch_start_time = time.time()

                total_cycles += 1

                # Update session with health info
                self._update_session_health(device_health)

                # Sleep based on task schedule
                cycle_duration = time.time() - cycle_start
                sleep_time = max(0, self._get_cycle_interval() - cycle_duration)
                if sleep_time > 0:
                    time.sleep(sleep_time)

        except KeyboardInterrupt:
            self.logger.info("Acquisition interrupted by user")
        except Exception as e:
            self.logger.error(f"Acquisition loop failed: {e}", exc_info=True)
            raise
        finally:
            # Write any remaining buffered data
            if batch_buffer:
                try:
                    self._write_to_storage(batch_buffer)
                    total_points += len(batch_buffer)
                except Exception as e:
                    self.logger.error(f"Failed to write final batch: {e}")

            # Disconnect all protocols
            for device_id, protocol in device_protocols.items():
                if protocol:
                    try:
                        protocol.disconnect()
                        device = self.device_groups[device_id]["device"]
                        self.logger.info(f"Disconnected from device {device.code}")
                    except Exception as e:
                        self.logger.warning(f"Error disconnecting protocol: {e}")

            # Clean up storage connections
            for storage in self.storages.values():
                try:
                    storage.disconnect()
                except Exception as e:
                    self.logger.warning(f"Error disconnecting storage: {e}")

        return {
            "status": "completed",
            "total_cycles": total_cycles,
            "total_points": total_points,
            "errors": errors[-10:],  # Last 10 errors
            "device_health": device_health,
        }

    def _should_continue(self) -> bool:
        """Check if acquisition loop should continue."""
        # Refresh session from DB
        self.session.refresh_from_db()

        # Continue only if session is still running
        return self.session.status == acq_models.AcquisitionSession.STATUS_RUNNING

    def _get_cycle_interval(self) -> float:
        """Get acquisition cycle interval in seconds."""
        # Parse schedule (simple implementation)
        schedule = self.task.schedule
        if schedule == "continuous":
            return 1.0  # Default 1 second
        # Add more schedule parsing logic as needed
        return 1.0

    def _format_for_storage(
        self,
        readings: List[Dict[str, Any]],
        device: config_models.Device
    ) -> List[Dict[str, Any]]:
        """
        Format readings for storage backends.

        Args:
            readings: Raw protocol readings
            device: Device object

        Returns:
            Formatted data points
        """
        formatted = []

        for reading in readings:
            # Get point details
            point = self.task.points.filter(code=reading["code"]).first()
            if not point:
                continue

            # Prepare data point
            # Use current time for timestamp (server-side time) instead of device timestamp
            # This ensures timestamps are always valid and in sync with the data collection system
            current_timestamp = int(time.time() * 1e9)  # Convert to nanoseconds

            data_point = {
                "measurement": device.metadata.get("device_a_tag", device.code) if device.metadata else device.code,
                "tags": {
                    "site": device.site.code,
                    "device": device.code,
                    "point": reading["code"],
                    "quality": reading.get("quality", "good"),
                },
                "fields": {
                    reading["code"]: reading["value"],
                },
                "time": current_timestamp,
            }

            # Add template info if available
            if point.template:
                data_point["tags"]["cn_name"] = point.template.name
                data_point["tags"]["unit"] = point.template.unit

            formatted.append(data_point)

        return formatted

    def _write_to_storage(self, data: List[Dict[str, Any]]) -> None:
        """Write data to all configured storage backends (InfluxDB)."""
        for storage_name, storage in self.storages.items():
            try:
                storage.write(data)
                self.logger.debug(f"Wrote {len(data)} points to {storage_name}")
            except Exception as e:
                self.logger.error(f"Failed to write to {storage_name}: {e}")

    def _update_session_health(self, device_health: Dict[int, Dict[str, Any]]) -> None:
        """
        Update session metadata with device health information.

        Args:
            device_health: Dict mapping device_id to health status
        """
        try:
            # Format health info for storage
            health_summary = {}
            for device_id, health in device_health.items():
                device = self.device_groups[device_id]["device"]
                health_summary[device.code] = {
                    "status": health["status"],
                    "consecutive_failures": health["consecutive_failures"],
                    "last_success": health["last_success"],
                }

            # Update session metadata
            self.session.metadata = self.session.metadata or {}
            self.session.metadata["device_health"] = health_summary
            self.session.metadata["last_health_update"] = time.time()
            self.session.save(update_fields=["metadata", "updated_at"])

        except Exception as e:
            self.logger.warning(f"Failed to update session health: {e}")
