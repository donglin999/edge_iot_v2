/**
 * 注册表的硬要求:**没登记专属界面的协议必须能正常工作,且前端零改动**。
 *
 * 后端加一个新协议时不会有人来动前端;如果 getProtocolConfig 对未知协议返回了
 * undefined,或者哪天有人把默认实现换成需要专属配置的东西,新协议的「添加设备」
 * 会直接白屏。这里就是拦这个的。
 */
import { describe, expect, it, vi } from 'vitest';

vi.mock('../services/apiClient', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  downloadFile: vi.fn().mockResolvedValue(undefined),
}));

import GenericProtocolConfig from './GenericProtocolConfig';
import ScadaConfig from './scada/ScadaConfig';
import { getProtocolConfig, hasCustomConfig, protocolsWithExcel } from './registry';

/** 后端当前登记的全部协议(backend/acquisition/protocols/)。 */
const BACKEND_PROTOCOLS = [
  'modbus_rtu',
  'modbus_tcp',
  'mqtt',
  'opcua',
  'scada',
  'siemens_s7',
];

describe('getProtocolConfig', () => {
  it('未登记的协议一律回落到通用表单,而不是 undefined', () => {
    for (const name of BACKEND_PROTOCOLS.filter((p) => p !== 'scada')) {
      const entry = getProtocolConfig(name);
      expect(entry.component, `${name} 没拿到配置组件`).toBe(GenericProtocolConfig);
      expect(entry.ownsBaseFields, `${name} 不该自带公共字段`).toBe(false);
    }
  });

  it('后端将来新增的协议(前端完全没见过)也能拿到可渲染的组件', () => {
    const entry = getProtocolConfig('some_future_protocol_v9');
    expect(entry.component).toBe(GenericProtocolConfig);
    expect(entry.width).toBeGreaterThan(0);
  });

  it('协议名缺失时也不炸(表单刚打开还没选协议)', () => {
    expect(getProtocolConfig(undefined).component).toBe(GenericProtocolConfig);
  });

  it('scada 走专属界面,并自带设备名/站点(所以宿主不再渲染公共字段)', () => {
    const entry = getProtocolConfig('scada');
    expect(entry.component).toBe(ScadaConfig);
    expect(entry.ownsBaseFields).toBe(true);
    // 批量网格要宽一些,不然测点表挤成一团
    expect(entry.width).toBeGreaterThan(getProtocolConfig('modbus_tcp').width);
  });
});

describe('hasCustomConfig', () => {
  it('只有登记过的协议才算有专属界面', () => {
    expect(hasCustomConfig('scada')).toBe(true);
    expect(hasCustomConfig('modbus_tcp')).toBe(false);
    expect(hasCustomConfig(undefined)).toBe(false);
  });
});

describe('protocolsWithExcel', () => {
  it('scada 声明了自己的两表模板 —— 通用 40 列单表模板对它是错的格式', () => {
    const entries = protocolsWithExcel();
    const scada = entries.find((e) => e.protocol === 'scada');
    expect(scada).toBeDefined();
    expect(scada?.label).toMatch(/两表/);
    expect(typeof scada?.download).toBe('function');
  });

  it('没声明模板的协议不会混进来(否则设备管理页会列出下不动的项)', () => {
    expect(protocolsWithExcel().map((e) => e.protocol)).not.toContain('modbus_tcp');
  });
});
