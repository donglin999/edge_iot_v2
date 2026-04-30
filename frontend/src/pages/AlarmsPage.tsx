/**
 * Alarms — list active alarms, edit threshold rules.
 */
import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Button,
  Card,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Segmented,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  CheckOutlined,
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import { apiClient } from '../services/apiClient';

const { Title, Text } = Typography;

interface AlarmRule {
  id?: number;
  name: string;
  point_code: string;
  device_code: string;
  operator: 'gt' | 'ge' | 'lt' | 'le' | 'eq' | 'ne' | 'between' | 'outside';
  threshold: number | null;
  threshold_high: number | null;
  severity: 'info' | 'warning' | 'critical';
  is_active: boolean;
  description: string;
}

interface Alarm {
  id: number;
  rule: number;
  rule_name: string;
  severity: 'info' | 'warning' | 'critical';
  point_code: string;
  device_code: string;
  value: unknown;
  status: 'firing' | 'acked' | 'cleared';
  fired_at: string;
  message: string;
}

const SEVERITY_COLORS: Record<string, string> = {
  info: 'blue',
  warning: 'gold',
  critical: 'red',
};

const STATUS_LABEL: Record<string, { label: string; color: string }> = {
  firing: { label: '未确认', color: 'red' },
  acked: { label: '已确认', color: 'orange' },
  cleared: { label: '已恢复', color: 'green' },
};

const AlarmsPage = () => {
  const [alarms, setAlarms] = useState<Alarm[]>([]);
  const [rules, setRules] = useState<AlarmRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<'all' | 'firing' | 'acked' | 'cleared'>('all');
  const [tab, setTab] = useState<'alarms' | 'rules'>('alarms');
  const [ruleModalOpen, setRuleModalOpen] = useState(false);
  const [editingRule, setEditingRule] = useState<AlarmRule | null>(null);
  const [form] = Form.useForm();

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [alarmsRes, rulesRes] = await Promise.all([
        apiClient.get<Alarm[]>('/acquisition/alarms/', {
          params: statusFilter === 'all' ? undefined : { status: statusFilter },
        }),
        apiClient.get<AlarmRule[]>('/acquisition/alarm-rules/'),
      ]);
      setAlarms(alarmsRes.data);
      setRules(rulesRes.data);
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 15000);
    return () => clearInterval(interval);
  }, [refresh]);

  const ackAlarm = async (id: number) => {
    await apiClient.post(`/acquisition/alarms/${id}/ack/`);
    message.success('已确认');
    refresh();
  };

  const openRuleModal = (rule?: AlarmRule) => {
    setEditingRule(rule || null);
    if (rule) {
      form.setFieldsValue(rule);
    } else {
      form.resetFields();
      form.setFieldsValue({ operator: 'gt', severity: 'warning', is_active: true });
    }
    setRuleModalOpen(true);
  };

  const saveRule = async () => {
    let values: AlarmRule;
    try {
      values = (await form.validateFields()) as AlarmRule;
    } catch {
      return;
    }
    if (editingRule?.id) {
      await apiClient.patch(`/acquisition/alarm-rules/${editingRule.id}/`, values);
      message.success('规则已更新');
    } else {
      await apiClient.post('/acquisition/alarm-rules/', values);
      message.success('规则已创建');
    }
    setRuleModalOpen(false);
    refresh();
  };

  const deleteRule = (rule: AlarmRule) => {
    Modal.confirm({
      title: `删除规则 "${rule.name}" ?`,
      content: '已产生的告警记录会保留。',
      okType: 'danger',
      onOk: async () => {
        await apiClient.delete(`/acquisition/alarm-rules/${rule.id}/`);
        message.success('已删除');
        refresh();
      },
    });
  };

  const alarmColumns: ColumnsType<Alarm> = [
    {
      title: '触发时间',
      dataIndex: 'fired_at',
      width: 180,
      render: (v: string) => new Date(v).toLocaleString('zh-CN'),
    },
    {
      title: '严重度',
      dataIndex: 'severity',
      width: 90,
      render: (s: string) => <Tag color={SEVERITY_COLORS[s]}>{s.toUpperCase()}</Tag>,
    },
    { title: '规则', dataIndex: 'rule_name' },
    { title: '设备', dataIndex: 'device_code' },
    { title: '测点', dataIndex: 'point_code', render: (c: string) => <code>{c}</code> },
    {
      title: '触发值',
      dataIndex: 'value',
      render: (v) => <Text strong>{String(v)}</Text>,
    },
    {
      title: '状态',
      dataIndex: 'status',
      render: (s: string) => (
        <Badge status={s === 'firing' ? 'error' : s === 'acked' ? 'warning' : 'success'}
               text={STATUS_LABEL[s]?.label ?? s} />
      ),
    },
    {
      title: '操作',
      width: 100,
      render: (_v, row) =>
        row.status === 'firing' ? (
          <Button size="small" icon={<CheckOutlined />} onClick={() => ackAlarm(row.id)}>
            确认
          </Button>
        ) : null,
    },
  ];

  const ruleColumns: ColumnsType<AlarmRule> = [
    { title: '名称', dataIndex: 'name' },
    {
      title: '匹配',
      render: (_v, r) => (
        <Text style={{ fontFamily: 'monospace' }}>
          {r.device_code || '*'}/{r.point_code}
        </Text>
      ),
    },
    {
      title: '条件',
      render: (_v, r) => {
        const opMap = {
          gt: '>', ge: '≥', lt: '<', le: '≤', eq: '=', ne: '≠',
          between: '∈', outside: '∉',
        };
        return r.operator === 'between' || r.operator === 'outside'
          ? `${opMap[r.operator]} [${r.threshold}, ${r.threshold_high}]`
          : `${opMap[r.operator]} ${r.threshold}`;
      },
    },
    {
      title: '严重度',
      dataIndex: 'severity',
      width: 90,
      render: (s: string) => <Tag color={SEVERITY_COLORS[s]}>{s.toUpperCase()}</Tag>,
    },
    {
      title: '启用',
      dataIndex: 'is_active',
      width: 80,
      render: (v: boolean) => (v ? <Badge status="success" text="启用" /> : <Badge status="default" text="停用" />),
    },
    {
      title: '操作',
      width: 140,
      align: 'right',
      render: (_v, row) => (
        <Space size="small">
          <Button size="small" icon={<EditOutlined />} onClick={() => openRuleModal(row)} />
          <Button size="small" danger icon={<DeleteOutlined />} onClick={() => deleteRule(row)} />
        </Space>
      ),
    },
  ];

  const firingCount = alarms.filter((a) => a.status === 'firing').length;

  return (
    <div style={{ padding: 24 }}>
      <Card variant="borderless" style={{ marginBottom: 16 }}>
        <Space style={{ width: '100%', justifyContent: 'space-between' }}>
          <div>
            <Title level={3} style={{ margin: 0 }}>
              告警中心
            </Title>
            <Text type="secondary">
              基于阈值规则的实时告警。规则会被采集循环每秒评估,触发即写入告警记录并推 WebSocket。
            </Text>
          </div>
          <Space>
            <Button icon={<ReloadOutlined />} onClick={refresh}>
              刷新
            </Button>
            {tab === 'rules' && (
              <Button type="primary" icon={<PlusOutlined />} onClick={() => openRuleModal()}>
                新建规则
              </Button>
            )}
          </Space>
        </Space>
      </Card>

      {firingCount > 0 && tab === 'alarms' && (
        <Alert
          type="error"
          showIcon
          message={`当前有 ${firingCount} 个未确认告警`}
          style={{ marginBottom: 16 }}
        />
      )}

      <Card variant="borderless">
        <Tabs
          activeKey={tab}
          onChange={(k) => setTab(k as 'alarms' | 'rules')}
          items={[
            {
              key: 'alarms',
              label: `告警记录 (${alarms.length})`,
              children: (
                <>
                  <Space style={{ marginBottom: 16 }}>
                    <Segmented
                      value={statusFilter}
                      onChange={(v) => setStatusFilter(v as typeof statusFilter)}
                      options={[
                        { label: '全部', value: 'all' },
                        { label: '未确认', value: 'firing' },
                        { label: '已确认', value: 'acked' },
                        { label: '已恢复', value: 'cleared' },
                      ]}
                    />
                  </Space>
                  <Table<Alarm>
                    rowKey="id"
                    columns={alarmColumns}
                    dataSource={alarms}
                    loading={loading}
                    locale={{ emptyText: <Empty description="无告警" /> }}
                    pagination={{ pageSize: 15 }}
                  />
                </>
              ),
            },
            {
              key: 'rules',
              label: `阈值规则 (${rules.length})`,
              children: (
                <Table<AlarmRule>
                  rowKey="id"
                  columns={ruleColumns}
                  dataSource={rules}
                  loading={loading}
                  locale={{ emptyText: <Empty description="尚未创建任何规则,点击右上「新建规则」开始" /> }}
                  pagination={{ pageSize: 15 }}
                />
              ),
            },
          ]}
        />
      </Card>

      <Modal
        open={ruleModalOpen}
        title={editingRule?.id ? '编辑规则' : '新建规则'}
        onOk={saveRule}
        onCancel={() => setRuleModalOpen(false)}
        okText="保存"
        cancelText="取消"
        destroyOnClose
        width={600}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="规则名称" rules={[{ required: true }]}>
            <Input placeholder="例如:温度过高告警" />
          </Form.Item>
          <Space style={{ width: '100%' }} size="large">
            <Form.Item name="point_code" label="测点编码" rules={[{ required: true }]}
                       style={{ width: 240 }}>
              <Input placeholder="例如:temperature" />
            </Form.Item>
            <Form.Item name="device_code" label="设备编码(留空匹配所有)"
                       style={{ width: 240 }}>
              <Input placeholder="选填" />
            </Form.Item>
          </Space>
          <Space style={{ width: '100%' }} size="large">
            <Form.Item name="operator" label="操作符" rules={[{ required: true }]}
                       style={{ width: 160 }}>
              <Select
                options={[
                  { label: '> 大于', value: 'gt' },
                  { label: '≥ 大于等于', value: 'ge' },
                  { label: '< 小于', value: 'lt' },
                  { label: '≤ 小于等于', value: 'le' },
                  { label: '= 等于', value: 'eq' },
                  { label: '≠ 不等于', value: 'ne' },
                  { label: '∈ 区间内', value: 'between' },
                  { label: '∉ 区间外', value: 'outside' },
                ]}
              />
            </Form.Item>
            <Form.Item name="threshold" label="阈值" rules={[{ required: true }]}
                       style={{ width: 160 }}>
              <InputNumber style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="threshold_high" label="阈值上限(区间)"
                       style={{ width: 160 }}>
              <InputNumber style={{ width: '100%' }} />
            </Form.Item>
          </Space>
          <Form.Item name="severity" label="严重度">
            <Select
              options={[
                { label: '🔵 info 提示', value: 'info' },
                { label: '🟡 warning 警告', value: 'warning' },
                { label: '🔴 critical 严重', value: 'critical' },
              ]}
            />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default AlarmsPage;
