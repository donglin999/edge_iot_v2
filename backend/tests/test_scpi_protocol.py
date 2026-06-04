"""XIU-142 SCPI 串口协议 (LINO 安规测试仪) 单测。

覆盖:结束标记 (NL/^END) · 全部指令族的构造与解析 · 仿真器查询应答 ·
PDF 示例命令 (PARA:STEP_1:6_..., SOUR:STEP_1:3, FETC:RESU_?) · SCPIProtocol loopback 端到端。
"""
import pytest

from acquisition.protocols import ProtocolRegistry
from acquisition.protocols import scpi
from acquisition.protocols.scpi import (
    EOI_MARKER,
    LinoSafetyTester,
    ScpiError,
    SCPIProtocol,
    TERMINATOR_NL,
    build_disp_page,
    build_fetc_query_auto,
    build_fetc_query_result,
    build_fetc_set_auto,
    build_mmem_query_group,
    build_mmem_set_group,
    build_mmem_status,
    build_para_set_step,
    build_safe_start,
    build_safe_stop,
    build_sour_query_step,
    build_sour_set_step,
    decode,
    encode,
    normalize_params,
    parse_command,
    parse_results,
)


# ---------------------------------------------------------------------------
# 结束标记
# ---------------------------------------------------------------------------


class TestTerminator:
    def test_encode_default_nl(self):
        assert encode("SAFE:STAR") == b"SAFE:STAR\x0a"
        assert encode("SAFE:STAR").endswith(b"\x0a")

    def test_encode_eoi(self):
        out = encode("SAFE:STAR", use_eoi=True)
        assert out == b"SAFE:STAR" + EOI_MARKER.encode() + TERMINATOR_NL

    def test_decode_strips_terminator_and_eoi(self):
        assert decode(b"MMEM:STAT\x0a") == "MMEM:STAT"
        assert decode(b"SAFE:STAR^END\x0a") == "SAFE:STAR"
        assert decode(b"  0\r\n") == "0"


# ---------------------------------------------------------------------------
# 构造器 → wire 字符串
# ---------------------------------------------------------------------------


class TestBuilders:
    def test_safe(self):
        assert build_safe_start() == "SAFE:STAR"
        assert build_safe_stop() == "SAFE:STOP"

    def test_disp_page(self):
        assert build_disp_page("TEST") == "DISP:PAGE TEST"
        assert build_disp_page("syst") == "DISP:PAGE SYST"
        with pytest.raises(ScpiError):
            build_disp_page("BOGUS")

    def test_sour(self):
        # PDF 示例: SOUR:STEP_1:3 (下划线=空格)
        assert build_sour_set_step(1, 3) == "SOUR:STEP 1:3"
        assert build_sour_query_step(1) == "SOUR:STEP 1:?"
        with pytest.raises(ScpiError):
            build_sour_set_step(9, 3)   # 步越界
        with pytest.raises(ScpiError):
            build_sour_set_step(1, 9)   # 项目越界

    def test_para_pdf_example(self):
        # PDF 示例: PARA:STEP_1:6_220_0_0_0_4_0.6_2100_0.03_3
        wire = build_para_set_step(1, 6, ["220", "0", "0", "0", "4", "0.6", "2100", "0.03", "3"])
        assert wire == "PARA:STEP 1:6 220 0 0 0 4 0.6 2100 0.03 3"

    def test_para_backslash_means_zero(self):
        wire = build_para_set_step(2, 1, ["\\", "10", "\\"])
        # \ → 0, 不足 9 个补 0
        assert wire == "PARA:STEP 2:1 0 10 0 0 0 0 0 0 0"

    def test_normalize_params_overflow(self):
        with pytest.raises(ScpiError):
            normalize_params(["1"] * 10)

    def test_mmem_group_zero_padded(self):
        assert build_mmem_set_group(1) == "MMEM:GROU 01"
        assert build_mmem_set_group(99) == "MMEM:GROU 99"
        assert build_mmem_query_group() == "MMEM:GROU ?"
        with pytest.raises(ScpiError):
            build_mmem_set_group(100)

    def test_fetc(self):
        assert build_fetc_set_auto(1) == "FETC:AUTO 1"
        assert build_fetc_set_auto(0) == "FETC:AUTO 0"
        assert build_fetc_query_auto() == "FETC:AUTO ?"
        assert build_fetc_query_result() == "FETC:RESU ?"
        assert build_mmem_status() == "MMEM:STAT"


# ---------------------------------------------------------------------------
# 解析 (容错: _ 或空格)
# ---------------------------------------------------------------------------


class TestParse:
    def test_parse_underscore_as_space(self):
        # 文档原文用下划线
        cmd = parse_command(b"PARA:STEP_1:6_220_0_0_0_4_0.6_2100_0.03_3\x0a")
        assert cmd.subsystem == "PARA"
        assert cmd.node == "STEP"
        assert cmd.args[0] == "1:6"
        assert cmd.args[1:] == ("220", "0", "0", "0", "4", "0.6", "2100", "0.03", "3")
        assert not cmd.is_query

    def test_parse_query(self):
        cmd = parse_command(b"FETC:RESU_?\x0a")
        assert cmd.subsystem == "FETC"
        assert cmd.node == "RESU"
        assert cmd.is_query

    def test_parse_sour_query_step(self):
        cmd = parse_command("SOUR:STEP 1:?")
        assert cmd.is_query
        assert cmd.args[0] == "1:?"

    def test_parse_rejects_no_subsystem(self):
        with pytest.raises(ScpiError):
            parse_command("GARBAGE")


# ---------------------------------------------------------------------------
# 仿真器应答
# ---------------------------------------------------------------------------


class TestSimulator:
    def test_status_default_ready(self):
        sim = LinoSafetyTester()
        assert sim.handle(encode(build_mmem_status())) == b"0\x0a"

    def test_safe_start_sets_testing(self):
        sim = LinoSafetyTester()
        assert sim.handle(encode(build_safe_start())) is None
        assert sim.handle(encode(build_mmem_status())) == b"1\x0a"

    def test_auto_default_off_and_toggle(self):
        sim = LinoSafetyTester()
        assert sim.handle(encode(build_fetc_query_auto())) == b"0\x0a"
        sim.handle(encode(build_fetc_set_auto(1)))
        assert sim.handle(encode(build_fetc_query_auto())) == b"1\x0a"

    def test_group_switch_and_query(self):
        sim = LinoSafetyTester()
        assert sim.handle(encode(build_mmem_query_group())) == b"01\x0a"
        sim.handle(encode(build_mmem_set_group(7)))
        assert sim.handle(encode(build_mmem_query_group())) == b"07\x0a"

    def test_sour_set_then_query(self):
        sim = LinoSafetyTester()
        sim.handle(encode(build_sour_set_step(1, 3)))
        assert sim.handle(encode(build_sour_query_step(1))) == b"STEP 1:3\x0a"
        # 未设置的步返回项目 0
        assert sim.handle(encode(build_sour_query_step(2))) == b"STEP 2:0\x0a"

    def test_para_set_then_group_query(self):
        sim = LinoSafetyTester()
        sim.handle(encode(build_para_set_step(1, 6, ["220", "0", "0", "0", "4", "0.6", "2100", "0.03", "3"])))
        sim.handle(encode(build_sour_set_step(2, 3)))
        resp = decode(sim.handle(encode("PARA:STEP ?")))
        # 8 步项目, 步1=6 步2=3 其余 0
        assert resp == "STEP 1:6 2:3 3:0 4:0 5:0 6:0 7:0 8:0"

    def test_resu_empty(self):
        sim = LinoSafetyTester()
        assert decode(sim.handle(encode(build_fetc_query_result()))) == "STEP 0:0 0 0 0 0"

    def test_resu_with_results(self):
        sim = LinoSafetyTester()
        sim.handle(encode(build_para_set_step(1, 3, ["2100"])))
        sim.handle(encode(build_para_set_step(2, 1, ["10"])))
        sim.load_result(1, 3, ["2100", "0.01", "0"], status=2)
        sim.load_result(2, 1, ["10", "0.1", "0"], status=3)
        resp = decode(sim.handle(encode(build_fetc_query_result())))
        assert resp == "STEP 1:3 2100 0.01 0 2;STEP 2:1 10 0.1 0 3"

    def test_unknown_subsystem_raises(self):
        sim = LinoSafetyTester()
        with pytest.raises(ScpiError):
            sim.handle(encode("XXXX:YYYY"))


# ---------------------------------------------------------------------------
# RESU 应答解析 (controller 侧)
# ---------------------------------------------------------------------------


class TestParseResults:
    def test_roundtrip(self):
        raw = "STEP 1:3 2100 0.01 0 2;STEP 2:1 10 0.1 0 3"
        results = parse_results(raw)
        assert len(results) == 2
        assert results[0].step == 1 and results[0].item == 3 and results[0].status == 2
        assert results[1].step == 2 and results[1].item == 1 and results[1].status == 3

    def test_empty_placeholder_skipped(self):
        assert parse_results("STEP 0:0 0 0 0 0") == []


# ---------------------------------------------------------------------------
# SCPIProtocol loopback 端到端
# ---------------------------------------------------------------------------


class TestProtocolRegistration:
    def test_registered(self):
        assert "scpi" in ProtocolRegistry.list_protocols()
        # 别名
        assert ProtocolRegistry.get("lino") is SCPIProtocol
        assert ProtocolRegistry.get("scpi_serial") is SCPIProtocol

    def test_describe_has_fields(self):
        d = ProtocolRegistry.describe("scpi")
        assert d["name"] == "scpi"
        names = {f["name"] for f in d["device_fields"]}
        assert "serial_port" in names and "use_eoi" in names


class TestProtocolLoopback:
    def _connect(self, **extra):
        cfg = {"serial_port": "loopback"}
        cfg.update(extra)
        p = ProtocolRegistry.create("scpi", cfg)
        assert p.connect()
        return p

    def test_health_check(self):
        p = self._connect()
        assert p.health_check()

    def test_status_point(self):
        p = self._connect()
        readings = p.read_points([{"code": "st", "query": "status"}])
        assert readings[0]["value"] == 0 and readings[0]["quality"] == "good"
        p.start_test()
        readings = p.read_points([{"code": "st", "query": "status"}])
        assert readings[0]["value"] == 1

    def test_program_and_read_results(self):
        p = self._connect()
        p.set_step_params(1, 6, ["220", "0", "0", "0", "4", "0.6", "2100", "0.03", "3"])
        p.set_step_item(2, 3)
        # 注入测试结果到底层仿真器
        sim = p.transport.sim
        sim.load_result(1, 6, ["220", "0.5", "0"], status=2)
        sim.load_result(2, 3, ["2100", "0.02", "0"], status=2)
        results = p.read_results()
        assert {r.step for r in results} == {1, 2}
        assert all(r.status == 2 for r in results)

    def test_step_status_point(self):
        p = self._connect()
        p.set_step_item(1, 3)
        p.transport.sim.load_result(1, 3, ["2100", "0", "0"], status=3)
        readings = p.read_points([{"code": "s1", "query": "step_status", "step": 1}])
        assert readings[0]["value"] == 3

    def test_group_field_switches_on_connect(self):
        p = self._connect(group=5)
        readings = p.read_points([{"code": "g", "query": "group"}])
        assert readings[0]["value"] == 5

    def test_auto_result_field_on_connect(self):
        p = self._connect(auto_result=True)
        readings = p.read_points([{"code": "a", "query": "auto"}])
        assert readings[0]["value"] == 1

    def test_eoi_mode_roundtrip(self):
        p = self._connect(use_eoi=True)
        assert p.health_check()  # 仿真器照样解析带 ^END 的指令

    def test_disconnect(self):
        p = self._connect()
        p.disconnect()
        assert not p.is_connected
