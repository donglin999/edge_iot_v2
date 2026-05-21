/**
 * Fleet page (XIU-51 / M1).
 *
 * - List registered edge nodes with status / version / last_seen / labels.
 * - 5s polling, AbortController-cancelled on unmount or re-fetch.
 * - Factory-register modal — surfaces the one-shot activation_token exactly
 *   once after a successful POST.
 *
 * Realtime via /ws/fleet/ is M2; polling is sufficient for M1.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Empty,
  Form,
  Input,
  Modal,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { CopyOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import axios from 'axios';
import dayjs from 'dayjs';
import relativeTime from 'dayjs/plugin/relativeTime';
import 'dayjs/locale/zh-cn';

import {
  listEdges,
  registerEdge,
  type EdgeNode,
  type EdgeStatus,
} from '../services/fleet';
import './FleetPage.css';

dayjs.extend(relativeTime);
dayjs.locale('zh-cn');

const { Text, Title, Paragraph } = Typography;

const REFRESH_INTERVAL_MS = 5000;

const STATUS_META: Record<EdgeStatus, { label: string; color: string }> = {
  online: { label: '在线', color: 'success' },
  offline: { label: '离线', color: 'default' },
  pending: { label: '待激活', color: 'warning' },
};

function formatLastSeen(value: string | null): string {
  if (!value) return '从未上报';
  const dt = dayjs(value);
  if (!dt.isValid()) return value;
  return dt.fromNow();
}

interface RegisterResult {
  name: string;
  token: string;
}

const FleetPage = () => {
  const [edges, setEdges] = useState<EdgeNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [modalOpen, setModalOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [registerResult, setRegisterResult] = useState<RegisterResult | null>(null);
  const [form] = Form.useForm<{ name: string; labelsText?: string }>();

  // Track the in-flight controller so each refresh cancels the previous one
  // (mount/unmount and 5s tick both go through the same path).
  const abortRef = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const list = await listEdges(controller.signal);
      if (!controller.signal.aborted) {
        setEdges(list);
      }
    } catch (err) {
      if (axios.isCancel(err) || controller.signal.aborted) return;
      // Axios interceptor already surfaces a message; just keep prior data.
    } finally {
      if (!controller.signal.aborted) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, REFRESH_INTERVAL_MS);
    return () => {
      window.clearInterval(timer);
      abortRef.current?.abort();
    };
  }, [refresh]);

  const counts = useMemo(() => {
    const c = { online: 0, offline: 0, pending: 0 };
    for (const e of edges) c[e.status] += 1;
    return c;
  }, [edges]);

  const columns: ColumnsType<EdgeNode> = [
    {
      title: '名称',
      dataIndex: 'name',
      render: (name: string) => <strong>{name}</strong>,
      sorter: (a, b) => a.name.localeCompare(b.name),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 110,
      render: (status: EdgeStatus) => {
        const meta = STATUS_META[status] ?? { label: status, color: 'default' };
        return <Tag color={meta.color}>{meta.label}</Tag>;
      },
      filters: (['online', 'offline', 'pending'] as EdgeStatus[]).map((s) => ({
        text: STATUS_META[s].label,
        value: s,
      })),
      onFilter: (value, row) => row.status === value,
    },
    {
      title: '版本',
      dataIndex: 'version',
      width: 140,
      render: (version: string) =>
        version ? (
          <Text style={{ fontFamily: 'monospace' }}>{version}</Text>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: '最后心跳',
      dataIndex: 'last_seen',
      width: 200,
      render: (value: string | null) => (
        <Tooltip title={value ?? '从未上报'}>
          <Text type={value ? undefined : 'secondary'}>{formatLastSeen(value)}</Text>
        </Tooltip>
      ),
      sorter: (a, b) => {
        const av = a.last_seen ? dayjs(a.last_seen).valueOf() : 0;
        const bv = b.last_seen ? dayjs(b.last_seen).valueOf() : 0;
        return av - bv;
      },
    },
    {
      title: '标签',
      dataIndex: 'labels',
      render: (labels: Record<string, string> | null | undefined) => {
        const entries = Object.entries(labels ?? {});
        if (entries.length === 0) return <Text type="secondary">—</Text>;
        return (
          <Space size={[4, 4]} wrap>
            {entries.map(([k, v]) => (
              <Tag key={k}>
                {k}: {v}
              </Tag>
            ))}
          </Space>
        );
      },
    },
  ];

  const parseLabels = (raw?: string): Record<string, string> | undefined => {
    if (!raw) return undefined;
    const out: Record<string, string> = {};
    const items = raw
      .split(/[,\n]/)
      .map((s) => s.trim())
      .filter(Boolean);
    for (const item of items) {
      const eq = item.indexOf('=');
      if (eq <= 0) {
        throw new Error(`标签格式不正确："${item}"，应为 key=value`);
      }
      const k = item.slice(0, eq).trim();
      const v = item.slice(eq + 1).trim();
      if (!k) throw new Error(`标签格式不正确："${item}"`);
      out[k] = v;
    }
    return Object.keys(out).length ? out : undefined;
  };

  const handleRegister = async () => {
    let values: { name: string; labelsText?: string };
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    let labels: Record<string, string> | undefined;
    try {
      labels = parseLabels(values.labelsText);
    } catch (err) {
      message.error((err as Error).message);
      return;
    }
    setSubmitting(true);
    try {
      const created = await registerEdge({ name: values.name.trim(), labels });
      setRegisterResult({ name: created.name, token: created.activation_token });
      form.resetFields();
      setModalOpen(false);
      refresh();
    } catch {
      // interceptor showed an error message already
    } finally {
      setSubmitting(false);
    }
  };

  const copyToken = async () => {
    if (!registerResult) return;
    try {
      await navigator.clipboard.writeText(registerResult.token);
      message.success('激活 token 已复制');
    } catch {
      message.warning('无法访问剪贴板,请手动复制');
    }
  };

  return (
    <div style={{ padding: 24 }}>
      <Card variant="borderless" style={{ marginBottom: 16 }}>
        <Space style={{ width: '100%', justifyContent: 'space-between', flexWrap: 'wrap' }}>
          <div>
            <Title level={3} style={{ margin: 0 }}>
              边缘节点
            </Title>
            <Text type="secondary">
              在线 {counts.online} · 离线 {counts.offline} · 待激活 {counts.pending} · 共 {edges.length}
            </Text>
          </div>
          <Space wrap>
            <Button icon={<ReloadOutlined />} onClick={refresh}>
              刷新
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => {
                form.resetFields();
                setModalOpen(true);
              }}
            >
              注册新 edge
            </Button>
          </Space>
        </Space>
      </Card>

      <Card variant="borderless">
        <Table<EdgeNode>
          rowKey="id"
          columns={columns}
          dataSource={edges}
          loading={loading}
          locale={{
            emptyText: (
              <Empty
                description='暂无边缘节点 — 点击"注册新 edge"以接入第一台设备'
                image={Empty.PRESENTED_IMAGE_SIMPLE}
              />
            ),
          }}
          pagination={{ pageSize: 20, showSizeChanger: true }}
        />
      </Card>

      <Modal
        title="注册新 edge"
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={handleRegister}
        okText="注册"
        cancelText="取消"
        confirmLoading={submitting}
        destroyOnClose
      >
        <Form form={form} layout="vertical" preserve={false} requiredMark>
          <Form.Item
            name="name"
            label="名称"
            rules={[
              { required: true, message: '请输入名称' },
              { max: 128, message: '名称最长 128 字符' },
            ]}
          >
            <Input placeholder="例如:edge-shanghai-01" autoFocus />
          </Form.Item>
          <Form.Item
            name="labelsText"
            label="标签(可选)"
            help='每行或逗号分隔,格式 key=value,例如:site=shanghai, role=gateway'
          >
            <Input.TextArea rows={3} placeholder="site=shanghai, role=gateway" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="一次性激活 Token"
        open={Boolean(registerResult)}
        onCancel={() => setRegisterResult(null)}
        onOk={() => setRegisterResult(null)}
        okText="我已保存"
        cancelButtonProps={{ style: { display: 'none' } }}
        destroyOnClose
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="此 token 只展示一次,关闭后无法再次查看。请立刻保存或下发到 edge 设备。"
        />
        {registerResult ? (
          <>
            <Paragraph>
              <Text strong>edge 名称:</Text> {registerResult.name}
            </Paragraph>
            <div className="fleet-token-row">
              <Text code copyable={false} className="fleet-token-row__value">
                {registerResult.token}
              </Text>
              <Tooltip title="复制">
                <Button icon={<CopyOutlined />} onClick={copyToken} />
              </Tooltip>
            </div>
          </>
        ) : null}
      </Modal>
    </div>
  );
};

export default FleetPage;
