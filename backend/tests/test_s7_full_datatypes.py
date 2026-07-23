"""Siemens S7 —— 数据类型/地址解析穷举 + 真服务器 e2e 覆盖。

三层覆盖,由「真实程度」从高到低:

1. ``TestRealSnap7Server`` —— 真 python-snap7 服务器
   (``acquisition/testing/s7_mock_server.py``,底层真 snap7 C 库 +
   ISO-on-TCP)+ 真 ``SiemensS7Protocol``(真 ``snap7.client.Client``)。
   本机若没装 python-snap7 会被 ``pytest.importorskip`` 跳过 —— 这是本文件
   唯一因未装库而降级的部分。

2. 字节级穷举 —— 直接对 ``_parse_s7_address`` / ``_decode`` 做参数化测试,
   不需要真 snap7:用 ``s7_with_area`` fixture(与 ``test_s7_lreal.py`` /
   ``test_protocol_hygiene.py`` 同一手法)把 ``_SNAP7_AVAILABLE`` 和 ``Area``
   换成假的,地址解析/解码逻辑本身仍是真代码在跑。

3. ``read_points`` 批量 + 断线语义 —— 用 ``tests/mocks/transports.py`` 的
   ``FakeS7Client``(替的是 snap7 库这一层,协议实现原样跑),按
   ``(db, start)`` 喂真实字节,验证多点混合类型批读、坏地址单点隔离、
   ``fail`` 开关驱动的连接/重连语义。

运行(仅本文件,不跑全量)::

    cd backend && python -m pytest tests/test_s7_full_datatypes.py -v
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mocks.transports import FakeArea, FakeS7Client  # noqa: E402

from acquisition.protocols import s7 as s7_mod  # noqa: E402
from acquisition.protocols.base import ConnectionError as S7ConnectionError  # noqa: E402
from acquisition.protocols.base import ReadError  # noqa: E402
from acquisition.protocols.s7 import SiemensS7Protocol, _decode, _parse_s7_address  # noqa: E402


# ===========================================================================
# 公共 fixture
# ===========================================================================


@pytest.fixture(autouse=True)
def _reset_fake_s7():
    """每条用例前后都把 FakeS7Client 恢复原状,用例互不影响。"""
    FakeS7Client.fail = False
    FakeS7Client.areas = {}
    yield
    FakeS7Client.fail = False
    FakeS7Client.areas = {}


@pytest.fixture
def s7_with_area(monkeypatch):
    """不装 python-snap7 也能跑 ``_parse_s7_address``:换一个假 ``Area`` 枚举。

    与 ``test_s7_lreal.py`` / ``test_protocol_hygiene.py`` 同一手法,三处
    fixture 定义故意保持一致(没有提到共享——都是各自文件内的小 fixture,
    保持独立不产生跨文件耦合)。
    """
    fake_area = SimpleNamespace(DB="DB", MK="MK", PE="PE", PA="PA")
    monkeypatch.setattr(s7_mod, "_SNAP7_AVAILABLE", True)
    monkeypatch.setattr(s7_mod, "Area", fake_area)
    return s7_mod


@pytest.fixture
def s7_with_fake_client(monkeypatch):
    """把 snap7 模块整体换成 FakeS7Client 版本,协议实现原样跑。"""
    fake_snap7 = type("snap7", (), {"client": type("c", (), {"Client": FakeS7Client})})
    monkeypatch.setattr(s7_mod, "snap7", fake_snap7)
    monkeypatch.setattr(s7_mod, "Area", FakeArea)
    monkeypatch.setattr(s7_mod, "_SNAP7_AVAILABLE", True)
    return s7_mod


# ===========================================================================
# 1. 真 snap7 服务器 e2e —— 装了 python-snap7 才跑,没装则跳过
# ===========================================================================

snap7 = pytest.importorskip("snap7", reason="python-snap7 未安装,跳过真服务器 e2e(降级到字节级穷举覆盖)")

from acquisition.testing.s7_mock_server import S7MockServer  # noqa: E402


@pytest.fixture
def real_server():
    """起一个真 S7 mock 服务器,端口冲突时在 15400-15499 段内顺延重试。"""
    srv = S7MockServer(host="127.0.0.1", port=15420, port_retries=20)
    port = srv.start()
    try:
        yield srv, port
    finally:
        srv.stop()


class TestRealSnap7Server:
    """真服务器 + 真客户端 —— 覆盖 connect/read_area 的真实往返和 ISO-on-TCP 握手。"""

    def test_connect_uses_configured_source_port(self, real_server):
        """回归:source_port 曾经被 __init__ 读进来却从没传给 client.connect()，
        导致真实设备/仿真器只要不在 102 端口就连不上（见 s7.py 的 connect()）。
        这里显式给一个非 102 端口，若 source_port 又被吞掉，这条用例会先在
        connect() 就超时/拒连而失败。"""
        srv, port = real_server
        assert port != 102
        proto = SiemensS7Protocol({
            "source_ip": "127.0.0.1", "source_port": port,
            "rack": 0, "slot": 1, "timeout": 2.0,
        })
        try:
            assert proto.connect() is True
            assert proto.health_check() is True
        finally:
            proto.disconnect()

    @pytest.mark.parametrize("area,addr,writer,write_value,expect_value,data_type", [
        ("DB", "DB1.DBB1", "write_bytes", b"\x2a", 42, "byte"),
        ("DB", "DB1.DBW2", "write_int16", -1234, -1234, "int16"),
        ("DB", "DB1.DBW2", "write_uint16", 60000, 60000, "uint16"),
        ("DB", "DB1.DBD4", "write_int32", -70000, -70000, "int32"),
        ("DB", "DB1.DBD4", "write_uint32", 4200000000, 4200000000, "uint32"),
        ("DB", "DB1.DBD4", "write_float32", 12.5, 12.5, "float32"),
        ("DB", "DB1.DBD8", "write_float64", 3.14159265358979, 3.14159265358979, "lreal"),
        ("DB", "DB1.DBD8", "write_float64", -2.5e100, -2.5e100, "float64"),
    ])
    def test_scalar_round_trip(self, real_server, area, addr, writer, write_value, expect_value, data_type):
        srv, port = real_server
        getattr(srv, writer)(area, _offset_of(addr), write_value)
        proto = SiemensS7Protocol({"source_ip": "127.0.0.1", "source_port": port, "rack": 0, "slot": 1})
        try:
            proto.connect()
            results = proto.read_points([{"code": "p", "address": addr, "data_type": data_type}])
        finally:
            proto.disconnect()
        assert results[0]["quality"] == "good"
        if isinstance(expect_value, float):
            assert results[0]["value"] == pytest.approx(expect_value, rel=1e-6)
        else:
            assert results[0]["value"] == expect_value

    def test_bool_bit_round_trip(self, real_server):
        srv, port = real_server
        srv.set_bit("DB", 0, 5, True)
        srv.set_bit("DB", 0, 2, False)
        proto = SiemensS7Protocol({"source_ip": "127.0.0.1", "source_port": port, "rack": 0, "slot": 1})
        try:
            proto.connect()
            results = proto.read_points([
                {"code": "bit5", "address": "DB1.DBX0.5", "data_type": "bool"},
                {"code": "bit2", "address": "DB1.DBX0.2", "data_type": "bool"},
            ])
        finally:
            proto.disconnect()
        by_code = {r["code"]: r for r in results}
        assert by_code["bit5"]["value"] is True
        assert by_code["bit2"]["value"] is False

    def test_string_capacity_is_length_minus_2_header_bytes(self, real_server):
        """记录真实行为(不是 bug 修复,是把 help_text 里"会截断"量化到具体字节数):
        S7 STRING 头占 2 字节(maxlen, actual_len)，地址区域给多少字节，可用
        正文就是「区域长度 - 2」。DBB(1 字节)、DBW(2 字节)因此连头都放不下，
        _decode 里 ``len(buf) < 2`` 分支直接返回空串 —— 不是异常，是静默空值。"""
        srv, port = real_server
        srv.write_string("DB", 20, "hello world", max_len=32)
        proto = SiemensS7Protocol({"source_ip": "127.0.0.1", "source_port": port, "rack": 0, "slot": 1})
        try:
            proto.connect()
            results = proto.read_points([
                {"code": "s_dbb", "address": "DB1.DBB20", "data_type": "string"},  # 1B < header, always ""
                {"code": "s_dbw", "address": "DB1.DBW20", "data_type": "string"},  # 2B == header only, always ""
                {"code": "s_dbd", "address": "DB1.DBD20", "data_type": "string"},  # 4B -> 2 usable chars
            ])
        finally:
            proto.disconnect()
        by_code = {r["code"]: r for r in results}
        assert by_code["s_dbb"]["value"] == ""
        assert by_code["s_dbw"]["value"] == ""
        assert by_code["s_dbd"]["value"] == "he"

    def test_mixed_batch_one_bad_address_does_not_void_others(self, real_server):
        srv, port = real_server
        srv.write_float32("DB", 4, 99.5)
        proto = SiemensS7Protocol({"source_ip": "127.0.0.1", "source_port": port, "rack": 0, "slot": 1})
        try:
            proto.connect()
            results = proto.read_points([
                {"code": "good", "address": "DB1.DBD4", "data_type": "float32"},
                {"code": "bad_bit", "address": "DB1.DBX0.9", "data_type": "bool"},  # bit>7 rejected
                {"code": "bad_syntax", "address": "not-an-address", "data_type": "uint16"},
            ])
        finally:
            proto.disconnect()
        by_code = {r["code"]: r for r in results}
        assert by_code["good"]["quality"] == "good"
        assert by_code["good"]["value"] == pytest.approx(99.5)
        assert by_code["bad_bit"]["quality"] == "bad"
        assert by_code["bad_syntax"]["quality"] == "bad"

    def test_disconnect_then_reconnect_against_real_server(self, real_server):
        srv, port = real_server
        proto = SiemensS7Protocol({"source_ip": "127.0.0.1", "source_port": port, "rack": 0, "slot": 1})
        assert proto.connect() is True
        proto.disconnect()
        assert proto.is_connected is False
        assert proto.health_check() is False
        # 重连应当照样成功（幂等：read_points 在未连接时会自己先 connect()）。
        results = proto.read_points([{"code": "p", "address": "DB1.DBB1", "data_type": "byte"}])
        assert results[0]["quality"] == "good"
        proto.disconnect()


def _offset_of(address: str) -> int:
    """从 'DB1.DBD4' 这类地址里取出字节偏移，供上面参数化用例直接调用
    ``S7MockServer.write_*`` 写内存（不走 _parse_s7_address，纯粹为了铺测试
    数据，和被测代码路径无关）。"""
    import re
    m = re.search(r"(\d+)(?:\.\d+)?$", address)
    assert m
    return int(m.group(1))


# ===========================================================================
# 2. 字节级穷举 —— _parse_s7_address 地址解析
# ===========================================================================


class TestAddressParsingAllAreas:
    """DB / M / I / Q 四大区域 × bit/byte/word/dword 四种宽度全覆盖。"""

    @pytest.mark.parametrize("addr,expect_area,expect_db,expect_byte,expect_bit,expect_len", [
        # DB 区
        ("DB1.DBX3.5", "DB", 1, 3, 5, 1),
        ("DB5.DBB10", "DB", 5, 10, 0, 1),
        ("DB2.DBW20", "DB", 2, 20, 0, 2),
        ("DB3.DBD30", "DB", 3, 30, 0, 4),
        # M（Merker / 位存储）区 —— 裸 M 是位形式，也接受 MX 显式写法
        ("M5.3", "MK", 0, 5, 3, 1),
        ("MX5.3", "MK", 0, 5, 3, 1),
        ("MB7", "MK", 0, 7, 0, 1),
        ("MW10", "MK", 0, 10, 0, 2),
        ("MD20", "MK", 0, 20, 0, 4),
        # I（输入 / Eingang）区
        ("I0.0", "PE", 0, 0, 0, 1),
        ("IX1.7", "PE", 0, 1, 7, 1),
        ("IB2", "PE", 0, 2, 0, 1),
        ("IW4", "PE", 0, 4, 0, 2),
        ("ID8", "PE", 0, 8, 0, 4),
        # Q（输出 / Ausgang）区
        ("Q0.1", "PA", 0, 0, 1, 1),
        ("QX3.6", "PA", 0, 3, 6, 1),
        ("QB5", "PA", 0, 5, 0, 1),
        ("QW6", "PA", 0, 6, 0, 2),
        ("QD12", "PA", 0, 12, 0, 4),
    ])
    def test_parses_area_db_byte_bit_length(
        self, s7_with_area, addr, expect_area, expect_db, expect_byte, expect_bit, expect_len,
    ):
        area, db, byte, bit, length = s7_with_area._parse_s7_address(addr)
        assert area == expect_area
        assert db == expect_db
        assert byte == expect_byte
        assert bit == expect_bit
        assert length == expect_len

    def test_case_insensitive(self, s7_with_area):
        upper = s7_with_area._parse_s7_address("db1.dbd0")
        lower = s7_with_area._parse_s7_address("DB1.DBD0")
        assert upper == lower

    @pytest.mark.parametrize("addr", ["", "  ", "XYZ99", "DB.DBX0.0", "DBX", "FOO1.2"])
    def test_unparseable_address_raises(self, s7_with_area, addr):
        with pytest.raises(ValueError, match="无法解析"):
            s7_with_area._parse_s7_address(addr)


class TestBitOffsetBoundary:
    """bool 位偏移 0-7 合法，>7 一律拒绝（L3，穷举到每一个整数位而不是抽样）。"""

    @pytest.mark.parametrize("bit", range(0, 8))
    def test_bit_0_to_7_accepted(self, s7_with_area, bit):
        area, db, byte, got_bit, length = s7_with_area._parse_s7_address(f"DB1.DBX3.{bit}")
        assert got_bit == bit
        assert length == 1

    @pytest.mark.parametrize("bit", [8, 9, 15, 20, 99])
    def test_bit_above_7_rejected_on_every_area(self, s7_with_area, bit):
        for addr in (f"DB1.DBX3.{bit}", f"M3.{bit}", f"I0.{bit}", f"Q0.{bit}"):
            with pytest.raises(ValueError, match="位偏移"):
                s7_with_area._parse_s7_address(addr)


class TestDoubleWordWidening:
    """DBD/MD/ID/QD：float64/lreal 提示把读长从 4 字节拓宽到 8 字节，起始字节不变。"""

    @pytest.mark.parametrize("prefix,area", [("DB1.DBD", "DB"), ("MD", "MK"), ("ID", "PE"), ("QD", "PA")])
    @pytest.mark.parametrize("data_type,expect_len", [
        (None, 4), ("", 4), ("int32", 4), ("uint32", 4), ("float32", 4),
        ("float64", 8), ("lreal", 8), ("FLOAT64", 8), ("LReal", 8),
    ])
    def test_dword_length_by_data_type_hint(self, s7_with_area, prefix, area, data_type, expect_len):
        addr = f"{prefix}16"
        if data_type is None:
            area_got, db, byte, bit, length = s7_with_area._parse_s7_address(addr)
        else:
            area_got, db, byte, bit, length = s7_with_area._parse_s7_address(addr, data_type)
        assert length == expect_len
        assert byte == 16  # 起始字节不受 data_type 提示影响


# ===========================================================================
# 3. 字节级穷举 —— _decode
# ===========================================================================


class TestDecodeScalarTypes:
    """每种类型用已知大端字节序列构造，断言 _decode 精确还原（含负数/边界值）。"""

    @pytest.mark.parametrize("value", [0, 1, -1, 32767, -32768, 12345, -12345])
    def test_int16_round_trip(self, value):
        buf = struct.pack(">h", value)
        assert _decode(buf, "int16", bit=0) == value

    @pytest.mark.parametrize("value", [0, 1, 65535, 32768, 12345])
    def test_uint16_round_trip(self, value):
        buf = struct.pack(">H", value)
        assert _decode(buf, "uint16", bit=0) == value

    @pytest.mark.parametrize("value", [0, 1, -1, 2147483647, -2147483648, -70000, 70000])
    def test_int32_round_trip(self, value):
        buf = struct.pack(">i", value)
        assert _decode(buf, "int32", bit=0) == value

    @pytest.mark.parametrize("value", [0, 1, 4294967295, 2147483648, 4200000000])
    def test_uint32_round_trip(self, value):
        buf = struct.pack(">I", value)
        assert _decode(buf, "uint32", bit=0) == value

    @pytest.mark.parametrize("value", [0.0, -0.0, 1.0, -1.0, 12.5, -12.5, 3.4e38, -3.4e38, 1e-38])
    def test_float32_round_trip(self, value):
        buf = struct.pack(">f", value)
        assert _decode(buf, "float32", bit=0) == pytest.approx(struct.unpack(">f", buf)[0])

    def test_float32_aliased_as_real(self):
        buf = struct.pack(">f", 7.25)
        assert _decode(buf, "real", bit=0) == pytest.approx(7.25)

    @pytest.mark.parametrize("value", [0.0, -0.0, 3.14159265358979, -2.5e100, 1.7e308, 5e-300])
    @pytest.mark.parametrize("spelling", ["float64", "lreal", "FLOAT64", "LReal"])
    def test_float64_lreal_round_trip(self, value, spelling):
        buf = struct.pack(">d", value)
        assert _decode(buf, spelling, bit=0) == pytest.approx(struct.unpack(">d", buf)[0])

    @pytest.mark.parametrize("value", [0, 1, 127, 200, 255])
    def test_byte_round_trip(self, value):
        assert _decode(bytes([value]), "byte", bit=0) == value

    @pytest.mark.parametrize("bit", range(0, 8))
    def test_bool_each_bit_position(self, bit):
        set_buf = bytes([1 << bit])
        clear_buf = bytes([(~(1 << bit)) & 0xFF])
        assert _decode(set_buf, "bool", bit=bit) is True
        assert _decode(clear_buf, "bool", bit=bit) is False

    def test_bool_ignores_other_bits(self):
        # 0b1010_1010: 偶数位清零、奇数位置位 —— 只关心目标位，不受相邻位干扰。
        buf = bytes([0b10101010])
        assert _decode(buf, "bool", bit=1) is True
        assert _decode(buf, "bool", bit=0) is False
        assert _decode(buf, "bool", bit=7) is True
        assert _decode(buf, "bool", bit=6) is False

    def test_unknown_data_type_falls_back_to_raw_byte_list(self):
        assert _decode(b"\x01\x02\x03", "unknown_type", bit=0) == [1, 2, 3]


class TestDecodeString:
    def test_normal_string(self):
        # maxlen=10, actual_len=5, "hello"
        buf = bytes([10, 5]) + b"hello" + b"\x00" * 3
        assert _decode(buf, "string", bit=0) == "hello"

    def test_string_actual_len_shorter_than_declared_capacity(self):
        buf = bytes([20, 2]) + b"hi" + b"\x00" * 16
        assert _decode(buf, "string", bit=0) == "hi"

    def test_empty_string(self):
        buf = bytes([10, 0]) + b"\x00" * 10
        assert _decode(buf, "string", bit=0) == ""

    def test_buffer_shorter_than_header_returns_empty_string_not_exception(self):
        """和数值类型不同：string 分支对过短 buffer 是静默返回 ''，不抛异常。"""
        assert _decode(b"", "string", bit=0) == ""
        assert _decode(b"\x05", "string", bit=0) == ""

    def test_actual_len_longer_than_buffer_is_clamped_by_slice(self):
        """actual_len 字节本身若被(错误地)写得比 buffer 还长，Python 切片不会
        越界抛异常，只会拿到能拿到的部分 —— 记录这个静默截断行为。"""
        buf = bytes([10, 200]) + b"ab"  # actual_len=200 但 buffer 里只有 2 个字符
        assert _decode(buf, "string", bit=0) == "ab"


class TestDecodeShortBufferRaises:
    """数值类型 buffer 长度不足时 —— struct.unpack 直接抛异常（不像 string 会兜底）。

    这一层直接对 _decode 探底；read_points 那一层会把这个异常接住、只废单点
    （见 TestReadPointsBatch 里的对应用例）。
    """

    @pytest.mark.parametrize("data_type,buf", [
        ("int16", b"\x01"),
        ("uint16", b"\x01"),
        ("int32", b"\x01\x02\x03"),
        ("uint32", b"\x01\x02\x03"),
        ("float32", b"\x01\x02\x03"),
        ("float64", b"\x01\x02\x03\x04\x05\x06\x07"),
        ("lreal", b""),
    ])
    def test_short_numeric_buffer_raises_struct_error(self, data_type, buf):
        with pytest.raises(struct.error):
            _decode(buf, data_type, bit=0)

    def test_bool_empty_buffer_raises_index_error(self):
        with pytest.raises(IndexError):
            _decode(b"", "bool", bit=0)

    def test_byte_empty_buffer_raises_index_error(self):
        with pytest.raises(IndexError):
            _decode(b"", "byte", bit=0)


# ===========================================================================
# 4. read_points —— FakeS7Client 批读 + 混合类型 + 坏地址隔离
# ===========================================================================


class TestReadPointsBatch:
    def test_mixed_types_single_batch(self, s7_with_fake_client):
        # (db, start) -> 真实字节；每个点各自的 data_type 决定 _decode 怎么切。
        FakeS7Client.areas = {
            (1, 0): struct.pack(">f", 88.25),        # DB1.DBD0 float32
            (1, 10): struct.pack(">h", -500),         # DB1.DBW10 int16
            (1, 20): bytes([200]),                    # DB1.DBB20 byte
            (1, 30): struct.pack(">d", 6.02214076e23),  # DB1.DBD30 lreal
        }
        proto = SiemensS7Protocol({"source_ip": "10.0.0.1"})
        proto.is_connected = True
        proto.client = FakeS7Client()

        results = proto.read_points([
            {"code": "f32", "address": "DB1.DBD0", "data_type": "float32"},
            {"code": "i16", "address": "DB1.DBW10", "data_type": "int16"},
            {"code": "byt", "address": "DB1.DBB20", "data_type": "byte"},
            {"code": "f64", "address": "DB1.DBD30", "data_type": "lreal"},
        ])
        by_code = {r["code"]: r for r in results}
        assert by_code["f32"]["value"] == pytest.approx(88.25)
        assert by_code["f32"]["quality"] == "good"
        assert by_code["i16"]["value"] == -500
        assert by_code["byt"]["value"] == 200
        assert by_code["f64"]["value"] == pytest.approx(6.02214076e23)

    def test_bool_point_mixed_with_numeric_points(self, s7_with_fake_client):
        FakeS7Client.areas = {
            (0, 0): bytes([0b00000100]),  # M0.2 = True, other bits False
            (1, 0): struct.pack(">H", 4242),
        }
        proto = SiemensS7Protocol({"source_ip": "10.0.0.1"})
        proto.is_connected = True
        proto.client = FakeS7Client()

        results = proto.read_points([
            {"code": "flag", "address": "M0.2", "data_type": "bool"},
            {"code": "cnt", "address": "DB1.DBW0", "data_type": "uint16"},
        ])
        by_code = {r["code"]: r for r in results}
        assert by_code["flag"]["value"] is True
        assert by_code["cnt"]["value"] == 4242

    def test_one_bad_address_syntax_does_not_void_batch(self, s7_with_fake_client):
        FakeS7Client.areas = {(1, 0): struct.pack(">f", 1.5)}
        proto = SiemensS7Protocol({"source_ip": "10.0.0.1"})
        proto.is_connected = True
        proto.client = FakeS7Client()

        results = proto.read_points([
            {"code": "good", "address": "DB1.DBD0", "data_type": "float32"},
            {"code": "malformed", "address": "not-an-address", "data_type": "uint16"},
            {"code": "bit_oob", "address": "DB1.DBX0.9", "data_type": "bool"},
        ])
        by_code = {r["code"]: r for r in results}
        assert by_code["good"]["quality"] == "good"
        assert by_code["good"]["value"] == pytest.approx(1.5)
        assert by_code["malformed"]["quality"] == "bad"
        assert by_code["malformed"]["value"] is None
        assert by_code["bit_oob"]["quality"] == "bad"

    def test_short_wire_response_marks_only_that_point_bad(self, s7_with_fake_client, monkeypatch):
        """设备真的少发了字节（不是 FakeS7Client 的自动补零）—— 用一个自定义
        client 模拟"网络截断"，_decode 里的 struct.error 应该只废这一个点。"""
        class _TruncatingClient(FakeS7Client):
            def read_area(self, area, db, start, size):
                if (db, start) == (1, 0):
                    return bytearray(b"\x01\x02")  # 要 4 字节，只给 2 字节
                return super().read_area(area, db, start, size)

        FakeS7Client.areas = {(1, 10): struct.pack(">h", 7)}
        proto = SiemensS7Protocol({"source_ip": "10.0.0.1"})
        proto.is_connected = True
        proto.client = _TruncatingClient()

        results = proto.read_points([
            {"code": "truncated", "address": "DB1.DBD0", "data_type": "float32"},
            {"code": "fine", "address": "DB1.DBW10", "data_type": "int16"},
        ])
        by_code = {r["code"]: r for r in results}
        assert by_code["truncated"]["quality"] == "bad"
        assert by_code["fine"]["quality"] == "good"
        assert by_code["fine"]["value"] == 7

    def test_all_points_bad_raises_read_error(self, s7_with_fake_client):
        proto = SiemensS7Protocol({"source_ip": "10.0.0.1"})
        proto.is_connected = True
        proto.client = FakeS7Client()
        FakeS7Client.fail = True

        with pytest.raises(ReadError):
            proto.read_points([
                {"code": "p1", "address": "DB1.DBD0", "data_type": "float32"},
                {"code": "p2", "address": "DB1.DBW10", "data_type": "int16"},
            ])


# ===========================================================================
# 5. 断线 / 重连语义 —— FakeS7Client.fail 开关
# ===========================================================================


class TestConnectionLifecycle:
    def test_connect_success_sets_is_connected(self, s7_with_fake_client):
        proto = SiemensS7Protocol({"source_ip": "10.0.0.1"})
        assert proto.connect() is True
        assert proto.is_connected is True
        assert proto.health_check() is True

    def test_connect_failure_raises_and_leaves_not_connected(self, s7_with_fake_client):
        FakeS7Client.fail = True
        proto = SiemensS7Protocol({"source_ip": "10.0.0.1"})
        with pytest.raises(S7ConnectionError):
            proto.connect()
        assert proto.is_connected is False
        assert proto.health_check() is False

    def test_read_points_auto_connects_when_not_yet_connected(self, s7_with_fake_client):
        FakeS7Client.areas = {(1, 0): struct.pack(">f", 3.0)}
        proto = SiemensS7Protocol({"source_ip": "10.0.0.1"})
        assert proto.is_connected is False
        results = proto.read_points([{"code": "p", "address": "DB1.DBD0", "data_type": "float32"}])
        assert proto.is_connected is True
        assert results[0]["quality"] == "good"

    def test_read_points_propagates_connection_error_when_device_unreachable(self, s7_with_fake_client):
        FakeS7Client.fail = True
        proto = SiemensS7Protocol({"source_ip": "10.0.0.1"})
        with pytest.raises(S7ConnectionError):
            proto.read_points([{"code": "p", "address": "DB1.DBD0", "data_type": "float32"}])

    def test_reconnect_after_recovery(self, s7_with_fake_client):
        """先断 -> 连不上；恢复后 -> 正常连上并能读数（fail 开关模拟设备复电）。"""
        FakeS7Client.fail = True
        proto = SiemensS7Protocol({"source_ip": "10.0.0.1"})
        with pytest.raises(S7ConnectionError):
            proto.connect()
        assert proto.is_connected is False

        FakeS7Client.fail = False
        FakeS7Client.areas = {(1, 0): struct.pack(">f", 9.0)}
        assert proto.connect() is True
        results = proto.read_points([{"code": "p", "address": "DB1.DBD0", "data_type": "float32"}])
        assert results[0]["value"] == pytest.approx(9.0)

    def test_disconnect_then_reconnect_via_fake_client(self, s7_with_fake_client):
        proto = SiemensS7Protocol({"source_ip": "10.0.0.1"})
        proto.connect()
        assert proto.is_connected is True
        proto.disconnect()
        assert proto.is_connected is False
        assert proto.client is None
        assert proto.connect() is True

    def test_mid_batch_disconnect_marks_only_the_point_hit_bad_not_reconnect_mid_call(self, s7_with_fake_client):
        """read_points 只在**调用开始时**未连接才尝试 connect()；同一次调用期间
        链路中途断开(fail 从 False 切到 True)不会触发自动重连 —— 已经开始的
        这一批里，断开之前读到的点仍然 good，断开之后的点标 bad（单点隔离，
        不是整批作废；只有当"这一批全部失败"时 read_points 才会抛
        ReadError，见 test_all_points_bad_raises_read_error）。重连要等下一次
        调用时 ``is_connected`` 已被判为可用/不可用后再决定。"""
        FakeS7Client.areas = {(1, 0): struct.pack(">h", 1)}
        proto = SiemensS7Protocol({"source_ip": "10.0.0.1"})
        proto.connect()

        real_read_area = proto.client.read_area

        def _flaky_read_area(area, db, start, size):
            if (db, start) == (1, 2):
                FakeS7Client.fail = True  # 模拟第二个点读到一半链路就断了
            return real_read_area(area, db, start, size)

        proto.client.read_area = _flaky_read_area
        results = proto.read_points([
            {"code": "p1", "address": "DB1.DBW0", "data_type": "int16"},
            {"code": "p2", "address": "DB1.DBW2", "data_type": "int16"},
        ])
        by_code = {r["code"]: r for r in results}
        assert by_code["p1"]["quality"] == "good"
        assert by_code["p1"]["value"] == 1
        assert by_code["p2"]["quality"] == "bad"
