/**
 * Add / edit a single device.
 *
 * Connection parameters render dynamically from the protocol's DEVICE_FIELDS,
 * so this component never has to be touched when a new protocol is added.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Form, Input, Modal, Select, Spin, message } from 'antd';
import { apiClient } from '../services/apiClient';
import { listProtocols, type ProtocolDescriptor } from '../services/protocolApi';
import ProtocolFieldsForm from './ProtocolFieldsForm';

interface DeviceFormValues {
  name: string;
  site: number;
  protocol: string;
  metadata: Record<string, unknown>;
}

interface Props {
  open: boolean;
  /** Provide an existing device id to edit instead of create. */
  deviceId?: number;
  /** Pre-selected protocol when adding from a context that knows the protocol. */
  defaultProtocol?: string;
  onClose: () => void;
  onSaved?: () => void;
}

const DeviceFormModal: React.FC<Props> = ({ open, deviceId, defaultProtocol, onClose, onSaved }) => {
  const [form] = Form.useForm();
  const [protocols, setProtocols] = useState<ProtocolDescriptor[]>([]);
  const [selected, setSelected] = useState<string | undefined>(defaultProtocol);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  // Load protocol descriptors once when modal opens
  useEffect(() => {
    if (!open) return;
    listProtocols()
      .then((data) => setProtocols(data))
      .catch(() => undefined);
  }, [open]);

  // Pre-fill when editing
  useEffect(() => {
    if (!open) return;
    if (!deviceId) {
      form.resetFields();
      if (defaultProtocol) {
        form.setFieldsValue({ protocol: defaultProtocol });
        setSelected(defaultProtocol);
      }
      return;
    }
    setLoading(true);
    apiClient
      .get(`/config/devices/${deviceId}/`)
      .then((res) => {
        const dev = res.data;
        form.setFieldsValue({
          name: dev.name,
          site: dev.site,
          protocol: dev.protocol,
          metadata: dev.metadata || {},
        });
        setSelected(dev.protocol);
      })
      .finally(() => setLoading(false));
  }, [open, deviceId, defaultProtocol, form]);

  const currentSchema = useMemo(
    () => protocols.find((p) => p.name === selected),
    [protocols, selected],
  );

  const handleProtocolChange = (value: string) => {
    setSelected(value);
    // Reset metadata when switching protocol so old keys don't leak through.
    form.setFieldsValue({ metadata: {} });
  };

  const handleOk = async () => {
    let values: DeviceFormValues;
    try {
      values = (await form.validateFields()) as DeviceFormValues;
    } catch {
      return;
    }

    setSubmitting(true);
    try {
      // ip_address / port are derived on the server from metadata; we still
      // send a minimum subset for backwards-compatible filtering.
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
        // Server requires a unique `code` — derive it from the identity fields.
        const identity = currentSchema?.identity_fields ?? [];
        const idParts = identity.map((f) => metadata[f]).filter((v) => v !== undefined && v !== '');
        const code = `${values.protocol}-${idParts.join('-')}`.replace(/[/:\s]/g, '_');
        await apiClient.post('/config/devices/', { ...payload, code });
        message.success('设备已创建');
      }
      onSaved?.();
      onClose();
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      title={deviceId ? '编辑设备' : '添加设备'}
      okText="保存"
      cancelText="取消"
      onOk={handleOk}
      onCancel={onClose}
      width={600}
      confirmLoading={submitting}
      destroyOnClose
    >
      <Spin spinning={loading}>
        <Form form={form} layout="vertical" preserve={false}>
          <Form.Item
            name="name"
            label="设备名称"
            rules={[{ required: true, message: '请输入设备名称' }]}
          >
            <Input placeholder="如:1#车间空压机" />
          </Form.Item>

          <Form.Item name="site" label="站点 ID" initialValue={1}
                     tooltip="新站点请先在站点管理页创建">
            <Input type="number" />
          </Form.Item>

          <Form.Item
            name="protocol"
            label="通信协议"
            rules={[{ required: true, message: '请选择协议' }]}
          >
            <Select
              placeholder="请选择协议"
              onChange={handleProtocolChange}
              options={protocols.map((p) => ({
                label: `${p.label} · ${p.description.split(',')[0]}`,
                value: p.name,
              }))}
            />
          </Form.Item>

          {currentSchema && (
            <ProtocolFieldsForm fields={currentSchema.device_fields} namePath={['metadata']} />
          )}
        </Form>
      </Spin>
    </Modal>
  );
};

export default DeviceFormModal;
