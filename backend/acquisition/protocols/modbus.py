"""Modbus TCP protocol implementation."""
from __future__ import annotations

import time
from typing import Any, Dict, List

import modbus_tk.defines as cst
from modbus_tk import modbus_tcp

from .base import BaseProtocol, ConnectionError, ProtocolRegistry, ReadError


@ProtocolRegistry.register("modbustcp")
@ProtocolRegistry.register("modbus_tcp")
@ProtocolRegistry.register("modbus")
class ModbusTCPProtocol(BaseProtocol):
    """
    Modbus TCP protocol adapter.

    Supports batch reading of continuous register addresses for optimization.
    """

    def __init__(self, device_config: Dict[str, Any]) -> None:
        super().__init__(device_config)
        self.ip = device_config.get("source_ip")
        self.port = device_config.get("source_port", 502)
        self.slave_addr = device_config.get("source_slave_addr", 1)
        self.timeout = device_config.get("timeout", 10)
        self.master = None
        self.register_groups = {}

    def connect(self) -> bool:
        """Establish Modbus TCP connection."""
        try:
            self.master = modbus_tcp.TcpMaster(
                host=self.ip,
                port=self.port,
                timeout_in_sec=self.timeout
            )
            self.is_connected = True
            self.logger.info(f"Connected to Modbus TCP device {self.ip}:{self.port}")
            return True
        except Exception as e:
            self.is_connected = False
            self.logger.error(f"Failed to connect to {self.ip}:{self.port}: {e}")
            raise ConnectionError(f"Modbus TCP connection failed: {e}") from e

    def disconnect(self) -> None:
        """Close Modbus TCP connection."""
        if self.master:
            try:
                self.master.close()
                self.is_connected = False
                self.logger.info(f"Disconnected from {self.ip}:{self.port}")
            except Exception as e:
                self.logger.warning(f"Error during disconnect: {e}")
            finally:
                self.master = None

    def read_points(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Read data from Modbus registers.

        Points are grouped by function code and continuous addresses
        for optimized batch reading.

        Args:
            points: List of point configs with:
                - code: str (point identifier)
                - address: int (register address)
                - type: int (Modbus function code: 3=holding, 4=input)
                - num: int (number of registers, default 1)

        Returns:
            List of data readings.
        """
        if not self.is_connected or not self.master:
            if not self.connect():
                raise ReadError("Not connected to Modbus device")

        # Group points by function code and continuous addresses
        groups = self._group_continuous_registers(points)
        results = []

        for func_code, register_groups in groups.items():
            for group in register_groups:
                try:
                    start_addr = group[0]["address"]
                    total_length = (group[-1]["address"] + group[-1].get("num", 1)) - start_addr

                    # Batch read
                    data = self.master.execute(
                        slave=self.slave_addr,
                        function_code=func_code,
                        starting_address=start_addr,
                        quantity_of_x=total_length
                    )

                    # Distribute data to individual points
                    offset = 0
                    for point in group:
                        num_registers = point.get("num", 1)
                        point_data = data[offset:offset + num_registers]
                        offset += num_registers

                        value = point_data[0] if num_registers == 1 else list(point_data)

                        results.append({
                            "code": point["code"],
                            "value": value,
                            "timestamp": time.time_ns(),
                            "quality": "good",
                            "address": point["address"],
                            "raw_data": point_data,
                        })

                except Exception as e:
                    self.logger.error(
                        f"Failed to read registers starting at {start_addr}, "
                        f"length {total_length}, func_code {func_code}: {e}"
                    )
                    # Mark all points in this group as bad quality
                    for point in group:
                        results.append({
                            "code": point["code"],
                            "value": None,
                            "timestamp": time.time_ns(),
                            "quality": "bad",
                            "error": str(e),
                        })

        return results

    def health_check(self) -> bool:
        """Check if connection is alive by attempting a simple read."""
        if not self.is_connected or not self.master:
            return False
        try:
            # Try reading a single holding register at address 0
            self.master.execute(
                slave=self.slave_addr,
                function_code=cst.READ_HOLDING_REGISTERS,
                starting_address=0,
                quantity_of_x=1
            )
            return True
        except Exception as e:
            self.logger.warning(f"Health check failed: {e}")
            return False

    def _group_continuous_registers(
        self, points: List[Dict[str, Any]]
    ) -> Dict[int, List[List[Dict[str, Any]]]]:
        """
        Group registers by function code and continuous address ranges.

        Args:
            points: List of point configurations

        Returns:
            Dict mapping function_code -> list of continuous register groups
        """
        # Group by function code
        function_groups: Dict[int, List[Dict[str, Any]]] = {}
        for point in points:
            # Parse function code - handle both numeric and string types
            type_val = point.get("type", 3)
            try:
                func_code = int(type_val)
            except (ValueError, TypeError):
                # If type is a string like 'INT16', use default (holding registers)
                func_code = 3

            if func_code not in function_groups:
                function_groups[func_code] = []

            # Normalize point data
            num_val = point.get("num", 1)
            try:
                num = int(num_val)
            except (ValueError, TypeError):
                num = 1

            # Parse address - handle Modbus address offsets
            raw_addr = int(point.get("address", point.get("source_addr", 0)))

            # Convert Modbus display address to actual register address
            # 40001-49999 (Holding Registers) -> 0-9998
            # 30001-39999 (Input Registers) -> 0-9998
            # 10001-19999 (Coils) -> 0-9998
            # 00001-09999 (Discrete Inputs) -> 0-9998
            if raw_addr >= 40001:
                actual_addr = raw_addr - 40001  # Holding registers
            elif raw_addr >= 30001:
                actual_addr = raw_addr - 30001  # Input registers
            elif raw_addr >= 10001:
                actual_addr = raw_addr - 10001  # Coils
            elif raw_addr >= 1:
                actual_addr = raw_addr - 1      # Discrete inputs or direct address
            else:
                actual_addr = raw_addr          # Already actual address (0-based)

            normalized = {
                "code": point["code"],
                "address": actual_addr,
                "num": num,
                "type": func_code,
            }
            function_groups[func_code].append(normalized)

        # Sort by address within each function code group
        for func_code in function_groups:
            function_groups[func_code].sort(key=lambda x: x["address"])

        # Group continuous addresses
        continuous_groups: Dict[int, List[List[Dict[str, Any]]]] = {}
        for func_code, regs in function_groups.items():
            continuous_groups[func_code] = []
            current_group = []

            for reg in regs:
                if not current_group:
                    current_group.append(reg)
                else:
                    last_reg = current_group[-1]
                    expected_addr = last_reg["address"] + last_reg["num"]

                    if reg["address"] == expected_addr:
                        # Continuous, add to current group
                        current_group.append(reg)
                    else:
                        # Gap detected, start new group
                        continuous_groups[func_code].append(current_group)
                        current_group = [reg]

            if current_group:
                continuous_groups[func_code].append(current_group)

        return continuous_groups
