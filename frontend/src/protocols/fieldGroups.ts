/**
 * 把协议描述符里的 device_fields 按「元数据」分组。
 *
 * 目标是让通用表单不再是一坨平铺的输入框,而不是给每个协议手写布局 ——
 * 分组规则只看后端 FieldSpec 已经给出的信息(identity_fields / required /
 * kind / default),因此新增协议无需改动这里。
 *
 * 规则:
 *   1. 连接参数 —— 出现在 `identity_fields` 里,或 `required === true`。
 *      这些字段决定「连到哪台设备」,是操作员必须想清楚的部分,排最前。
 *   2. 认证信息 —— `kind === 'secret'`,以及与之配对的用户名字段
 *      (同名前缀 + username,如 mqtt_password ↔ mqtt_username)。
 *   3. 协议参数 —— 其余「没有可用默认值」的字段(default 为 null/空串),
 *      后端没给默认值意味着操作员多半得自己填。
 *   4. 高级选项 —— 其余「已有可用默认值」的字段(timeout / qos / 字节序 …),
 *      默认折叠;编辑态如果发现某个值和默认值不一样,自动展开。
 */
import type { FieldSpec, ProtocolDescriptor } from '../services/protocolApi';

export interface FieldGroup {
  key: string;
  title: string;
  /** 分组下方的一句说明。 */
  hint?: string;
  fields: FieldSpec[];
  /** true 时渲染成默认折叠的 Collapse。 */
  collapsed: boolean;
}

/** 该字段有没有「拿来就能用」的默认值。 */
function hasUsableDefault(spec: FieldSpec): boolean {
  if (spec.default === null || spec.default === undefined) return false;
  if (typeof spec.default === 'string' && spec.default.trim() === '') return false;
  return true;
}

/**
 * 长文本字段(话题模板 / endpoint URL 之类)独占一行,其余两列排布。
 * 依据仍是元数据:默认值或示例明显长的字符串字段。
 */
export function isWideField(spec: FieldSpec): boolean {
  if (spec.kind !== 'string') return false;
  const sample = String(spec.default ?? spec.example ?? '');
  return sample.length > 20;
}

export function groupDeviceFields(descriptor: ProtocolDescriptor): FieldGroup[] {
  const identity = new Set(descriptor.identity_fields ?? []);
  const fields = descriptor.device_fields ?? [];

  const connection: FieldSpec[] = [];
  const credentials: FieldSpec[] = [];
  const protocolParams: FieldSpec[] = [];
  const advanced: FieldSpec[] = [];

  // 密码字段的前缀,用来把配对的用户名一起收进「认证信息」。
  const secretPrefixes = fields
    .filter((f) => f.kind === 'secret')
    .map((f) => f.name.replace(/password$/, ''));
  const isCredential = (spec: FieldSpec) =>
    spec.kind === 'secret' ||
    secretPrefixes.some((prefix) => spec.name === `${prefix}username`);

  for (const spec of fields) {
    if (identity.has(spec.name) || spec.required) {
      connection.push(spec);
    } else if (isCredential(spec)) {
      credentials.push(spec);
    } else if (hasUsableDefault(spec)) {
      advanced.push(spec);
    } else {
      protocolParams.push(spec);
    }
  }

  const groups: FieldGroup[] = [
    {
      key: 'connection',
      title: '连接参数',
      hint: '决定连到哪台设备 —— 必填项与设备身份字段',
      fields: connection,
      collapsed: false,
    },
    { key: 'credentials', title: '认证信息', fields: credentials, collapsed: false },
    { key: 'protocol', title: '协议参数', fields: protocolParams, collapsed: false },
    {
      key: 'advanced',
      title: '高级选项',
      hint: '这些字段后端已给出可用默认值,通常不用改',
      fields: advanced,
      collapsed: true,
    },
  ];

  return groups.filter((g) => g.fields.length > 0);
}

/**
 * 编辑态下:高级选项里只要有一个值与默认值不同,就自动展开,
 * 免得操作员看不到自己上次改过的配置。
 */
export function shouldExpandAdvanced(
  group: FieldGroup,
  metadata: Record<string, unknown> | undefined,
): boolean {
  if (!metadata) return false;
  return group.fields.some((spec) => {
    const current = metadata[spec.name];
    if (current === undefined) return false;
    return String(current) !== String(spec.default ?? '');
  });
}
