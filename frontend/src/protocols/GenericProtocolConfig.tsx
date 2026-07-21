/**
 * 默认的协议配置界面 —— 由后端 FieldSpec 元数据驱动,不含任何协议专属代码。
 *
 * 任何没有在 `registry.tsx` 里登记专属界面的协议(modbus_tcp / modbus_rtu /
 * mqtt / opcua / siemens_s7 …)都会落到这里,行为与重构前的
 * `<ProtocolFieldsForm fields={descriptor.device_fields} />` 完全一致,
 * 只是把字段按 `fieldGroups.ts` 的元数据规则分成了几段,并把「已有默认值」
 * 的高级选项默认折叠。
 *
 * 保存走标准的 `/config/devices/` POST/PATCH,`code` 依然由协议的
 * identity_fields 拼出。
 */
import React, { forwardRef, useImperativeHandle, useMemo } from 'react';
import { Col, Collapse, Divider, Row, Typography, message } from 'antd';

import ProtocolFieldsForm from '../components/ProtocolFieldsForm';
import { apiClient } from '../services/apiClient';
import type { FieldSpec } from '../services/protocolApi';
import { groupDeviceFields, isWideField, shouldExpandAdvanced } from './fieldGroups';
import type { ProtocolConfigHandle, ProtocolConfigProps } from './registry';

const { Text } = Typography;

interface BaseFormValues {
  name: string;
  site: number;
  protocol: string;
  metadata: Record<string, unknown>;
}

/** 把一组字段排成两列(长文本字段独占一行)。 */
const FieldRow: React.FC<{ fields: FieldSpec[] }> = ({ fields }) => (
  <Row gutter={16}>
    {fields.map((spec) => (
      <Col key={spec.name} span={isWideField(spec) ? 24 : 12}>
        <ProtocolFieldsForm fields={[spec]} namePath={['metadata']} />
      </Col>
    ))}
  </Row>
);

const GenericProtocolConfig = forwardRef<ProtocolConfigHandle, ProtocolConfigProps>(
  ({ descriptor, form, deviceId, device, onSaved }, ref) => {
    const groups = useMemo(() => groupDeviceFields(descriptor), [descriptor]);

    useImperativeHandle(ref, () => ({
      async submit() {
        let values: BaseFormValues;
        try {
          values = (await form.validateFields()) as BaseFormValues;
        } catch {
          return false;
        }

        // ip_address / port 由后端从 metadata 推导;这里仍带上一份最小子集,
        // 保持旧的列表筛选行为不变。
        const metadata = values.metadata || {};
        const payload = {
          name: values.name,
          site: values.site,
          protocol: values.protocol,
          metadata,
          ip_address:
            (metadata.source_ip as string) ||
            (metadata.endpoint_url as string) ||
            (metadata.serial_port as string) ||
            '',
          port: typeof metadata.source_port === 'number' ? metadata.source_port : null,
        };

        if (deviceId) {
          await apiClient.patch(`/config/devices/${deviceId}/`, payload);
          message.success('设备已更新');
        } else {
          // 服务端要求 code 唯一 —— 由协议的身份字段拼出。
          const identity = descriptor.identity_fields ?? [];
          const idParts = identity
            .map((f) => metadata[f])
            .filter((v) => v !== undefined && v !== '');
          const code = `${values.protocol}-${idParts.join('-')}`.replace(/[/:\s]/g, '_');
          await apiClient.post('/config/devices/', { ...payload, code });
          message.success('设备已创建');
        }
        onSaved?.();
        return true;
      },
    }));

    return (
      <>
        {groups.map((group) => {
          const expand = group.collapsed && shouldExpandAdvanced(group, device?.metadata);
          if (group.collapsed) {
            return (
              <Collapse
                key={group.key}
                size="small"
                style={{ marginBottom: 8 }}
                defaultActiveKey={expand ? [group.key] : []}
                items={[
                  {
                    key: group.key,
                    label: (
                      <span>
                        {group.title}
                        {group.hint && (
                          <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                            {group.hint}
                          </Text>
                        )}
                      </span>
                    ),
                    children: <FieldRow fields={group.fields} />,
                  },
                ]}
              />
            );
          }
          return (
            <React.Fragment key={group.key}>
              <Divider orientation="left" plain style={{ marginTop: 4 }}>
                {group.title}
                {group.hint && (
                  <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                    {group.hint}
                  </Text>
                )}
              </Divider>
              <FieldRow fields={group.fields} />
            </React.Fragment>
          );
        })}
      </>
    );
  },
);

GenericProtocolConfig.displayName = 'GenericProtocolConfig';

export default GenericProtocolConfig;
