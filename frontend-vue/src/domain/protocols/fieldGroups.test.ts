/**
 * 分组规则只准看后端已经给出的 FieldSpec 元数据 —— 这是「新增协议前端零改动」
 * 的前提。这里锁的就是这条:所有断言都只用 identity_fields / required /
 * kind / default 构造,一旦有人为了让某个协议好看而在 fieldGroups 里写死协议名,
 * 这些用例就会开始靠不住(也会有人来改它们,那就是信号)。
 */
import { describe, expect, it } from 'vitest';

import { groupDeviceFields, isWideField, shouldExpandAdvanced } from './fieldGroups';
import type { FieldSpec, ProtocolDescriptor } from '../../services/protocolApi';

function field(partial: Partial<FieldSpec> & { name: string }): FieldSpec {
  return {
    label: partial.name,
    kind: 'string',
    required: false,
    default: null,
    choices: null,
    help_text: '',
    example: null,
    ...partial,
  };
}

function descriptor(fields: FieldSpec[], identity: string[] = []): ProtocolDescriptor {
  return {
    name: 'demo',
    label: 'Demo',
    category: 'other',
    description: '',
    supports_pause: true,
    identity_fields: identity,
    device_fields: fields,
    point_fields: [],
  };
}

const groupOf = (d: ProtocolDescriptor, name: string) =>
  groupDeviceFields(d).find((g) => g.fields.some((f) => f.name === name))?.key;

describe('groupDeviceFields', () => {
  it('身份字段和必填字段进「连接参数」', () => {
    const d = descriptor(
      [field({ name: 'source_ip' }), field({ name: 'slave_id', required: true })],
      ['source_ip'],
    );
    expect(groupOf(d, 'source_ip')).toBe('connection');
    expect(groupOf(d, 'slave_id')).toBe('connection');
  });

  it('secret 字段和配对的用户名一起进「认证信息」', () => {
    const d = descriptor([
      field({ name: 'mqtt_password', kind: 'secret' }),
      field({ name: 'mqtt_username' }),
    ]);
    expect(groupOf(d, 'mqtt_password')).toBe('credentials');
    expect(groupOf(d, 'mqtt_username')).toBe('credentials');
  });

  it('不同前缀的用户名不会被别的密码字段吸过去', () => {
    const d = descriptor([
      field({ name: 'mqtt_password', kind: 'secret' }),
      field({ name: 'opcua_username' }),
    ]);
    expect(groupOf(d, 'opcua_username')).not.toBe('credentials');
  });

  it('有可用默认值的字段进默认折叠的「高级选项」', () => {
    const d = descriptor([field({ name: 'timeout', default: 5 })]);
    const groups = groupDeviceFields(d);
    expect(groupOf(d, 'timeout')).toBe('advanced');
    expect(groups.find((g) => g.key === 'advanced')?.collapsed).toBe(true);
  });

  it('default 是 false / 0 也算有默认值 —— 不能被当成「没填」', () => {
    const d = descriptor([
      field({ name: 'mqtt_use_tls', kind: 'bool', default: false }),
      field({ name: 'mqtt_qos', kind: 'int', default: 0 }),
    ]);
    expect(groupOf(d, 'mqtt_use_tls')).toBe('advanced');
    expect(groupOf(d, 'mqtt_qos')).toBe('advanced');
  });

  it('空字符串默认值不算有默认值,归「协议参数」等操作员填', () => {
    const d = descriptor([field({ name: 'mqtt_client_id', default: '  ' })]);
    expect(groupOf(d, 'mqtt_client_id')).toBe('protocol');
  });

  it('没有字段的分组不出现', () => {
    const d = descriptor([field({ name: 'source_ip', required: true })]);
    expect(groupDeviceFields(d).map((g) => g.key)).toEqual(['connection']);
  });

  it('每个字段有且只进一个分组,且一个都不能丢', () => {
    const names = ['source_ip', 'mqtt_password', 'mqtt_username', 'timeout', 'payload_path'];
    const d = descriptor(
      [
        field({ name: 'source_ip', required: true }),
        field({ name: 'mqtt_password', kind: 'secret' }),
        field({ name: 'mqtt_username' }),
        field({ name: 'timeout', default: 5 }),
        field({ name: 'payload_path' }),
      ],
      ['source_ip'],
    );
    const placed = groupDeviceFields(d).flatMap((g) => g.fields.map((f) => f.name));
    expect(placed.sort()).toEqual([...names].sort());
  });
});

describe('isWideField', () => {
  it('长文本(话题模板之类)独占一行', () => {
    expect(
      isWideField(
        field({
          name: 'topic_template',
          default: '/sys/{product_key}/device/{device_name}/thing/property/{code}/post',
        }),
      ),
    ).toBe(true);
  });

  it('短字符串和非字符串字段不独占', () => {
    expect(isWideField(field({ name: 'source_ip', example: '192.168.1.100' }))).toBe(false);
    expect(isWideField(field({ name: 'source_port', kind: 'int', default: 502 }))).toBe(false);
  });
});

describe('shouldExpandAdvanced', () => {
  const group = {
    key: 'advanced',
    title: '高级选项',
    collapsed: true,
    fields: [field({ name: 'timeout', default: 5 })],
  };

  it('值与默认值不同 → 自动展开,免得操作员看不到自己改过的配置', () => {
    expect(shouldExpandAdvanced(group, { timeout: 30 })).toBe(true);
  });

  it('值等于默认值 / 没有值 → 保持折叠', () => {
    expect(shouldExpandAdvanced(group, { timeout: 5 })).toBe(false);
    expect(shouldExpandAdvanced(group, {})).toBe(false);
    expect(shouldExpandAdvanced(group, undefined)).toBe(false);
  });
});
