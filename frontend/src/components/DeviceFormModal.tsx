/**
 * 添加 / 编辑一台设备。
 *
 * 这个弹窗本身只负责三件事:选协议、(通用协议的)公共字段、以及把「协议专属
 * 配置界面」挂进来。真正的表单与保存逻辑都在 `protocols/registry.tsx` 注册的
 * 配置组件里 —— 没登记专属界面的协议自动落到元数据驱动的通用表单,
 * 所以新增协议时这里零改动。
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Form, Input, Modal, Select, Spin, Typography } from 'antd';
import { apiClient } from '../services/apiClient';
import { listProtocols, type ProtocolDescriptor } from '../services/protocolApi';
import {
  getProtocolConfig,
  type DeviceRecord,
  type ProtocolConfigHandle,
} from '../protocols/registry';

const { Text } = Typography;

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
  const [device, setDevice] = useState<DeviceRecord | undefined>();
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const configRef = useRef<ProtocolConfigHandle>(null);

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
      setDevice(undefined);
      setSelected(defaultProtocol);
      if (defaultProtocol) {
        form.setFieldsValue({ protocol: defaultProtocol });
      }
      return;
    }
    setLoading(true);
    apiClient
      .get(`/config/devices/${deviceId}/`)
      .then((res) => {
        const dev = res.data as DeviceRecord;
        setDevice(dev);
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

  // Modal 的内容是延迟挂载的(destroyOnClose + Portal),打开瞬间那次
  // setFieldsValue 有可能写在表单项注册之前,导致「通信协议」明明已由
  // 「添加设备」下拉选好、表单里却是空值、一保存就报「请选择协议」。
  // 这里以 selected 为准再同步一次(protocols 载入后必定已挂载)。
  useEffect(() => {
    if (open && selected) form.setFieldValue('protocol', selected);
  }, [open, selected, protocols, form]);

  const currentSchema = useMemo(
    () => protocols.find((p) => p.name === selected),
    [protocols, selected],
  );

  const entry = useMemo(() => getProtocolConfig(selected), [selected]);
  const ConfigComponent = entry.component;

  const handleProtocolChange = (value: string) => {
    setSelected(value);
    // Reset metadata when switching protocol so old keys don't leak through.
    form.setFieldsValue({ metadata: {} });
  };

  const handleOk = async () => {
    setSubmitting(true);
    try {
      const saved = await configRef.current?.submit();
      if (saved) onClose();
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
      width={entry.width}
      confirmLoading={submitting}
      destroyOnClose
    >
      <Spin spinning={loading}>
        <Form form={form} layout="vertical" preserve={false}>
          {!entry.ownsBaseFields && (
            <>
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
            </>
          )}

          <Form.Item
            name="protocol"
            label="通信协议"
            rules={[{ required: true, message: '请选择协议' }]}
            // 协议决定了下面渲染哪套配置界面,编辑时不允许中途换协议。
            extra={entry.hint ? <Text type="secondary">{entry.hint}</Text> : undefined}
          >
            <Select
              placeholder="请选择协议"
              disabled={Boolean(deviceId)}
              onChange={handleProtocolChange}
              options={protocols.map((p) => ({
                label: `${p.label} · ${p.description.split(',')[0]}`,
                value: p.name,
              }))}
            />
          </Form.Item>

          {currentSchema && selected && (!deviceId || device) && (
            <ConfigComponent
              key={`${selected}-${deviceId ?? 'new'}`}
              ref={configRef}
              protocol={selected}
              descriptor={currentSchema}
              mode={deviceId ? 'edit' : 'create'}
              deviceId={deviceId}
              device={device}
              form={form}
              onSaved={onSaved}
              onRequestClose={onClose}
            />
          )}
        </Form>
      </Spin>
    </Modal>
  );
};

export default DeviceFormModal;
