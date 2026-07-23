"""Mitsubishi PLC (MC protocol) implementation."""
from __future__ import annotations

import re
import struct
import time
from typing import Any, Dict, List

from .base import (
    BaseProtocol,
    ConnectionError,
    FieldSpec,
    ProtocolMeta,
    ProtocolRegistry,
    ReadError,
)

#: Data types actually handled by ``_group_by_type`` -> ``_read_*`` dispatch
#: below. Note the string branch key is ``"str"``, not ``"string"``.
_MC_DATA_TYPES = ("int16", "int32", "float", "bool", "str")


@ProtocolRegistry.register("mc")
@ProtocolRegistry.register("plc")
class MitsubishiPLCProtocol(BaseProtocol):
    """
    Mitsubishi PLC MC protocol adapter.

    Supports various data types: int16, int32, float, float2, bool, string, hex
    Implements batch reading for continuous registers.
    """

    META = ProtocolMeta(
        name="mc",
        label="三菱 MC",
        category="industrial-ethernet",
        description="三菱 PLC MC 协议(Qna 兼容 3E 帧),以太网直连;按软元件地址(D/M/X/Y 等)读取。",
    )

    DEVICE_FIELDS = (
        FieldSpec("source_ip", "PLC IP", required=True, example="192.168.1.10"),
        FieldSpec("source_port", "端口", kind="int", default=6000, example=6000,
                  help_text="需与 PLC 侧 MC 协议端口设置一致,常见 6000/5000/8000"),
        FieldSpec("network_no", "网络号", kind="int", default=0,
                  help_text="MC 协议网络号,单机直连一般为 0"),
        FieldSpec("station_no", "站号", kind="int", default=0,
                  help_text="MC 协议站号,单机直连一般为 0"),
    )
    IDENTITY_FIELDS = ("source_ip", "source_port")

    POINT_FIELDS = (
        FieldSpec("code", "测点编码", required=True, example="temperature"),
        FieldSpec("address", "软元件地址", required=True, example="D100",
                  help_text="D/M 等软元件地址为十进制;X/Y 为八进制,如 X20、Y1A0"),
        FieldSpec("type", "数据类型", kind="enum", choices=_MC_DATA_TYPES, default="int16",
                  help_text="int16/int32/float/bool/str"),
        FieldSpec("num", "数量", kind="int", default=1,
                  help_text="寄存器/字符数量;str 类型表示读取的字(word)长度"),
        FieldSpec("coefficient", "系数", kind="float", default=1.0),
        FieldSpec("precision", "精度(小数位)", kind="int", default=0),
    )

    def __init__(self, device_config: Dict[str, Any]) -> None:
        super().__init__(device_config)
        self.ip = device_config.get("source_ip")
        self.port = device_config.get("source_port", 6000)
        self.network_no = int(device_config.get("network_no", 0) or 0)
        self.station_no = int(device_config.get("station_no", 0) or 0)
        self.plc = None

    def connect(self) -> bool:
        """Connect to Mitsubishi PLC."""
        try:
            # Import here to avoid hard dependency
            from lib.HslCommunication import MelsecMcNet

            self.plc = MelsecMcNet(self.ip, self.port)
            # network_no/station_no are best-effort: HslCommunication's
            # MelsecMcNet exposes them as plain attributes on some builds and
            # not others, so guard with hasattr rather than assuming the API.
            if hasattr(self.plc, "NetworkNumber"):
                self.plc.NetworkNumber = self.network_no
            if hasattr(self.plc, "NetworkStationNumber"):
                self.plc.NetworkStationNumber = self.station_no
            result = self.plc.ConnectServer()
            if result.IsSuccess:
                self.is_connected = True
                self.logger.info(f"Connected to PLC {self.ip}:{self.port}")
                return True
            else:
                raise ConnectionError(f"PLC connection failed: {result.Message}")
        except ImportError as e:
            raise ConnectionError(f"HslCommunication library not available: {e}") from e
        except Exception as e:
            self.is_connected = False
            self.logger.error(f"Failed to connect to PLC: {e}")
            raise ConnectionError(f"PLC connection error: {e}") from e

    def disconnect(self) -> None:
        """Close PLC connection."""
        if self.plc:
            try:
                self.plc.ConnectClose()
                self.is_connected = False
                self.logger.info(f"Disconnected from PLC {self.ip}:{self.port}")
            except Exception as e:
                self.logger.warning(f"Error during disconnect: {e}")
            finally:
                self.plc = None

    def read_points(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Read data from PLC registers.

        Points are grouped by data type for optimized reading.

        Args:
            points: List with:
                - code: str
                - address: str (e.g., 'D100', 'M10', 'X20')
                - type: str (data type: 'int16', 'int32', 'float', 'bool', etc.)
                - num: int (number of registers/bits)

        Returns:
            List of readings.
        """
        if not self.is_connected or not self.plc:
            if not self.connect():
                raise ReadError("Not connected to PLC")

        # Group by data type
        type_groups = self._group_by_type(points)
        results = []

        # Read each type group
        for data_type, type_points in type_groups.items():
            if data_type == "int16":
                results.extend(self._read_int16(type_points))
            elif data_type == "int32":
                results.extend(self._read_int32(type_points))
            elif data_type == "float":
                results.extend(self._read_float(type_points))
            elif data_type == "bool":
                results.extend(self._read_bool(type_points))
            elif data_type == "str":
                results.extend(self._read_string(type_points))
            else:
                # For unsupported types, mark as bad quality
                for point in type_points:
                    results.append({
                        "code": point["code"],
                        "value": None,
                        "timestamp": time.time_ns(),
                        "quality": "bad",
                        "error": f"Unsupported data type: {data_type}",
                    })

        return results

    def health_check(self) -> bool:
        """Check if PLC connection is alive."""
        if not self.is_connected or not self.plc:
            return False
        try:
            # Try a simple read to check health
            result = self.plc.ReadInt16("D0", 1)
            return result.IsSuccess
        except Exception as e:
            self.logger.warning(f"Health check failed: {e}")
            return False

    def _group_by_type(self, points: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        """Group points by data type."""
        groups = {}
        for point in points:
            data_type = point.get("type", "int16")
            if data_type not in groups:
                groups[data_type] = []
            groups[data_type].append(point)
        return groups

    def _read_int16(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Read int16 registers with batch optimization."""
        groups = self._group_continuous_registers(points, "D")
        results = []

        for group in groups:
            start_addr = group[0]["address"]
            # Word count spanning the group: from the first register's address
            # up to (and including) every word of the last register. Computed
            # as (last_addr + last_num) - first_addr so a single-register group
            # yields exactly 1 — never one short.
            total_length = (
                group[-1]["addr_num"] + group[-1].get("num", 1)
            ) - group[0]["addr_num"]

            try:
                result = self.plc.Read(start_addr, total_length)
                if result.IsSuccess:
                    for reg in group:
                        offset = reg["addr_num"] - group[0]["addr_num"]
                        value = result.Content[offset * 2] + result.Content[offset * 2 + 1] * 256
                        if value > 32767:  # Signed int16
                            value = value - 65536

                        coefficient = float(reg.get("coefficient", 1.0))
                        precision = int(reg.get("precision", 0))
                        value = int(round(value * coefficient, precision))

                        results.append({
                            "code": reg["code"],
                            "value": value,
                            "timestamp": time.time_ns(),
                            "quality": "good",
                        })
                else:
                    # Fallback to individual reads
                    for reg in group:
                        results.append(self._read_single_int16(reg))
            except Exception as e:
                self.logger.error(f"Batch read int16 failed at {start_addr}: {e}")
                for reg in group:
                    results.append(self._read_single_int16(reg))

        return results

    def _read_single_int16(self, point: Dict[str, Any]) -> Dict[str, Any]:
        """Read single int16 register."""
        try:
            result = self.plc.ReadInt16(point["address"], 1)
            if result.IsSuccess:
                value = result.Content[0]
                coefficient = float(point.get("coefficient", 1.0))
                precision = int(point.get("precision", 0))
                value = int(round(value * coefficient, precision))

                return {
                    "code": point["code"],
                    "value": value,
                    "timestamp": time.time_ns(),
                    "quality": "good",
                }
        except Exception as e:
            self.logger.error(f"Read int16 failed at {point['address']}: {e}")

        return {
            "code": point["code"],
            "value": None,
            "timestamp": time.time_ns(),
            "quality": "bad",
            "error": "Read failed",
        }

    def _read_int32(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Read int32 registers."""
        results = []
        for point in points:
            try:
                result = self.plc.ReadInt32(point["address"], 1)
                if result.IsSuccess:
                    value = result.Content[0]
                    coefficient = float(point.get("coefficient", 1.0))
                    precision = int(point.get("precision", 0))
                    value = round(value * coefficient, precision)

                    results.append({
                        "code": point["code"],
                        "value": value,
                        "timestamp": time.time_ns(),
                        "quality": "good",
                    })
                else:
                    raise ReadError("Read failed")
            except Exception as e:
                results.append({
                    "code": point["code"],
                    "value": None,
                    "timestamp": time.time_ns(),
                    "quality": "bad",
                    "error": str(e),
                })
        return results

    def _read_float(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Read float registers."""
        results = []
        for point in points:
            try:
                result = self.plc.ReadFloat(point["address"], 1)
                if result.IsSuccess:
                    value = result.Content[0]
                    coefficient = float(point.get("coefficient", 1.0))
                    precision = int(point.get("precision", 2))
                    value = round(value * coefficient, precision)

                    results.append({
                        "code": point["code"],
                        "value": value,
                        "timestamp": time.time_ns(),
                        "quality": "good",
                    })
                else:
                    raise ReadError("Read failed")
            except Exception as e:
                results.append({
                    "code": point["code"],
                    "value": None,
                    "timestamp": time.time_ns(),
                    "quality": "bad",
                    "error": str(e),
                })
        return results

    def _read_bool(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Read boolean registers."""
        results = []
        for point in points:
            try:
                result = self.plc.ReadBool(point["address"], 1)
                if result.IsSuccess:
                    results.append({
                        "code": point["code"],
                        "value": int(result.Content[0]),
                        "timestamp": time.time_ns(),
                        "quality": "good",
                    })
                else:
                    raise ReadError("Read failed")
            except Exception as e:
                results.append({
                    "code": point["code"],
                    "value": None,
                    "timestamp": time.time_ns(),
                    "quality": "bad",
                    "error": str(e),
                })
        return results

    def _read_string(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Read string registers."""
        results = []
        for point in points:
            try:
                num = int(point.get("num", 1))
                result = self.plc.ReadString(point["address"], num)
                if result.IsSuccess:
                    value = str(result.Content)
                    results.append({
                        "code": point["code"],
                        "value": value.strip(),
                        "timestamp": time.time_ns(),
                        "quality": "good",
                    })
                else:
                    raise ReadError("Read failed")
            except Exception as e:
                results.append({
                    "code": point["code"],
                    "value": None,
                    "timestamp": time.time_ns(),
                    "quality": "bad",
                    "error": str(e),
                })
        return results

    def _group_continuous_registers(
        self, points: List[Dict[str, Any]], prefix: str = "D"
    ) -> List[List[Dict[str, Any]]]:
        """Group continuous registers for batch reading."""
        # Parse address numbers
        for point in points:
            addr = point.get("address", point.get("source_addr", ""))
            if addr.startswith(prefix):
                try:
                    point["addr_num"] = int(addr[len(prefix) :])
                except ValueError:
                    point["addr_num"] = -1
            else:
                point["addr_num"] = -1

        # Filter and sort
        valid_points = [p for p in points if p["addr_num"] != -1]
        valid_points.sort(key=lambda x: x["addr_num"])

        if not valid_points:
            return []

        # Group continuous addresses
        groups = []
        current_group = [valid_points[0]]

        for i in range(1, len(valid_points)):
            prev = current_group[-1]
            curr = valid_points[i]
            expected_addr = prev["addr_num"] + prev.get("num", 1)

            if curr["addr_num"] <= expected_addr:
                current_group.append(curr)
            else:
                groups.append(current_group)
                current_group = [curr]

        if current_group:
            groups.append(current_group)

        return groups
