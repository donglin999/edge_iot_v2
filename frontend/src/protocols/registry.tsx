/**
 * 采集协议配置界面注册表。
 *
 * 背景:不同协议的「配置一台设备」流程差别很大。绝大多数协议(modbus_tcp /
 * modbus_rtu / mqtt / opcua / siemens_s7)只是填一组连接参数,后端 FieldSpec
 * 已经把字段描述得很清楚,通用表单就够了;个别协议(scada)是「一份共享网关
 * 连接 + N 台设备 × M 个测点」的批量流程,需要专属界面。
 *
 * 于是:协议名 → 配置组件的注册表 + 一个默认实现。没登记的协议自动落到
 * `GenericProtocolConfig`(元数据驱动的通用表单),所以新增协议时前端零改动;
 * 想要专属界面时,只需在下面的 `REGISTRY` 里加一行。
 *
 * ── 组件契约 ───────────────────────────────────────────────────────────
 * 配置组件是一个 `forwardRef` 组件:
 *
 *   props  : ProtocolConfigProps —— 协议名、后端描述符、create/edit 模式、
 *            编辑时的设备 id 与设备对象、宿主 Modal 的 Form 实例、保存回调。
 *   ref    : ProtocolConfigHandle —— 必须实现 `submit(): Promise<boolean>`。
 *            宿主 Modal 的「保存」按钮调用它;返回 true 表示保存成功(Modal
 *            关闭),返回 false 表示校验未过(Modal 保持打开)。保存成功前
 *            组件自行调用 `onSaved`。抛错由宿主捕获并保持 Modal 打开。
 *
 * 注册项还能声明:
 *   width          —— 宿主 Modal 宽度(批量类界面通常要更宽)。
 *   ownsBaseFields —— true 表示「设备名称 / 站点 ID」由组件自己管,宿主不再
 *                     渲染这两个公共字段(scada 的设备名来自设备网格)。
 *   hint           —— 协议选择框下方的一句话说明。
 *
 * 组件负责自己的持久化:通用表单走 `/config/devices/`,scada 走网关的
 * `provision` 端点。宿主不假设任何一种。
 */
import type { ForwardRefExoticComponent, RefAttributes } from 'react';
import type { FormInstance } from 'antd';

import type { ProtocolDescriptor } from '../services/protocolApi';
import GenericProtocolConfig from './GenericProtocolConfig';
import ScadaConfig from './scada/ScadaConfig';

/** 编辑态传进来的设备(与 /config/devices/ 的序列化结果一致)。 */
export interface DeviceRecord {
  id: number;
  site: number;
  name: string;
  code: string;
  protocol: string;
  ip_address: string;
  port: number | null;
  metadata: Record<string, unknown>;
}

export interface ProtocolConfigProps {
  /** 协议名,如 'scada' / 'modbus_tcp'。 */
  protocol: string;
  /** 后端 /acquisition/protocols/ 返回的描述符。 */
  descriptor: ProtocolDescriptor;
  mode: 'create' | 'edit';
  /** 编辑态的设备 id。 */
  deviceId?: number;
  /** 编辑态的设备对象(宿主已拉取,组件不必再请求一次)。 */
  device?: DeviceRecord;
  /** 宿主 Modal 的 Form 实例;通用表单用它承载 name/site/protocol/metadata。 */
  form: FormInstance;
  /** 保存成功后通知宿主刷新列表。 */
  onSaved?: () => void;
}

export interface ProtocolConfigHandle {
  /** 持久化配置。true = 已保存(可关闭弹窗),false = 校验未过。 */
  submit: () => Promise<boolean>;
}

export type ProtocolConfigComponent = ForwardRefExoticComponent<
  ProtocolConfigProps & RefAttributes<ProtocolConfigHandle>
>;

export interface ProtocolConfigEntry {
  component: ProtocolConfigComponent;
  /** 宿主 Modal 宽度。 */
  width: number;
  /** 组件是否自带「设备名称 / 站点 ID」。 */
  ownsBaseFields: boolean;
  hint?: string;
}

/** 未登记专属界面的协议一律用它 —— 通用 FieldSpec 表单。 */
const DEFAULT_ENTRY: ProtocolConfigEntry = {
  component: GenericProtocolConfig,
  width: 680,
  ownsBaseFields: false,
};

/** 协议名 → 专属配置界面。加新协议界面就在这里加一行。 */
const REGISTRY: Record<string, ProtocolConfigEntry> = {
  scada: {
    component: ScadaConfig,
    width: 1040,
    ownsBaseFields: true,
    hint: 'MQTT 连接参数由网关共享,配一次即可;这里只填设备名和测点。',
  },
};

/** 取协议的配置界面;没登记则返回通用表单。 */
export function getProtocolConfig(protocol?: string): ProtocolConfigEntry {
  if (protocol && REGISTRY[protocol]) return REGISTRY[protocol];
  return DEFAULT_ENTRY;
}

/** 该协议是否有专属界面(供宿主决定提示文案)。 */
export function hasCustomConfig(protocol?: string): boolean {
  return Boolean(protocol && REGISTRY[protocol]);
}
