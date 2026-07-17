/**
 * 新建 / 编辑一个 SCADA 网关(共享的 MQTT 服务配置)。
 *
 * 这些字段以前要在每台 scada 设备上重复填一遍;收敛到网关后,整条产线只填一次。
 */
import React, { useEffect, useState } from 'react';
import { Alert, Col, Form, Input, InputNumber, Modal, Row, Select, Switch, message } from 'antd';

import {
  DEFAULT_TOPIC_TEMPLATE,
  GATEWAY_DEFAULTS,
  createGateway,
  updateGateway,
  type ScadaGateway,
  type ScadaGatewayInput,
} from '../services/scadaApi';

interface Props {
  open: boolean;
  /** 传入已有网关表示编辑,否则为新建。 */
  gateway?: ScadaGateway;
  onClose: () => void;
  onSaved?: (gateway: ScadaGateway) => void;
}

const QOS_OPTIONS = [
  { label: '0 — 最多一次', value: 0 },
  { label: '1 — 至少一次', value: 1 },
  { label: '2 — 恰好一次', value: 2 },
];

const ScadaGatewayFormModal: React.FC<Props> = ({ open, gateway, onClose, onSaved }) => {
  const [form] = Form.useForm<ScadaGatewayInput>();
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!open) return;
    if (gateway) {
      form.setFieldsValue(gateway);
    } else {
      form.resetFields();
    }
  }, [open, gateway, form]);

  const handleOk = async () => {
    let values: ScadaGatewayInput;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }

    setSubmitting(true);
    try {
      const saved = gateway
        ? await updateGateway(gateway.id, values)
        : await createGateway(values);
      message.success(gateway ? '网关已更新' : '网关已创建');
      onSaved?.(saved);
      onClose();
    } catch {
      // apiClient 拦截器已经弹过错误了,这里保持弹窗打开让用户改。
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      title={gateway ? `编辑网关 · ${gateway.name}` : '新建 SCADA 网关'}
      okText="保存"
      cancelText="取消"
      onOk={handleOk}
      onCancel={onClose}
      width={720}
      confirmLoading={submitting}
      destroyOnClose
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="这套连接参数由该网关下的所有设备共享,只需配置一次。"
      />
      <Form form={form} layout="vertical" preserve={false} initialValues={GATEWAY_DEFAULTS}>
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item
              name="code"
              label="网关编码"
              rules={[{ required: true, message: '请输入网关编码' }]}
              tooltip="唯一标识,设备编码会以此为前缀,如 scada-gw1-xxx"
            >
              <Input placeholder="如:gw1" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item
              name="name"
              label="网关名称"
              rules={[{ required: true, message: '请输入网关名称' }]}
            >
              <Input placeholder="如:中山小家电 SCADA 网关" />
            </Form.Item>
          </Col>
        </Row>

        <Row gutter={16}>
          <Col span={12}>
            <Form.Item
              name="source_ip"
              label="Broker 地址"
              rules={[{ required: true, message: '请输入 Broker 地址' }]}
            >
              <Input placeholder="如:mqtt.example.com" />
            </Form.Item>
          </Col>
          <Col span={6}>
            <Form.Item
              name="source_port"
              label="端口"
              rules={[{ required: true, message: '请输入端口' }]}
            >
              <InputNumber min={1} max={65535} style={{ width: '100%' }} />
            </Form.Item>
          </Col>
          <Col span={6}>
            <Form.Item name="mqtt_use_tls" label="启用 TLS" valuePropName="checked">
              <Switch checkedChildren="TLS" unCheckedChildren="明文" />
            </Form.Item>
          </Col>
        </Row>

        <Row gutter={16}>
          <Col span={8}>
            <Form.Item name="mqtt_username" label="用户名">
              <Input autoComplete="off" placeholder="留空表示匿名" />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="mqtt_password" label="密码">
              <Input.Password autoComplete="new-password" placeholder="留空表示匿名" />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="mqtt_qos" label="QoS">
              <Select options={QOS_OPTIONS} />
            </Form.Item>
          </Col>
        </Row>

        <Row gutter={16}>
          <Col span={12}>
            <Form.Item name="mqtt_client_id" label="ClientID" tooltip="留空自动生成">
              <Input placeholder="留空自动生成" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="mqtt_read_timeout" label="读取超时(秒)">
              <InputNumber min={0.1} step={0.5} style={{ width: '100%' }} />
            </Form.Item>
          </Col>
        </Row>

        <Form.Item
          name="product_key"
          label="产品 Key"
          rules={[{ required: true, message: '请输入产品 Key' }]}
          tooltip="话题中的 {product_key} 段"
        >
          <Input placeholder="如:a1B2c3D4" />
        </Form.Item>

        <Form.Item
          name="topic_template"
          label="话题模板"
          rules={[{ required: true, message: '请输入话题模板' }]}
          tooltip="支持 {product_key} {device_name} {code} 占位符;{code} 为逐测点编码"
          extra={`默认:${DEFAULT_TOPIC_TEMPLATE}`}
        >
          <Input style={{ fontFamily: 'monospace' }} />
        </Form.Item>
      </Form>
    </Modal>
  );
};

export default ScadaGatewayFormModal;
