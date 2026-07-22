/**
 * Device list with AntD Table.
 *
 * - Search + protocol filter (driven by the live protocol registry, so adding
 *   a new protocol on the backend automatically adds a filter chip here).
 * - Add / Edit through DeviceFormModal which renders dynamic per-protocol
 *   fields from the protocol's FieldSpec schema.
 * - Cascade-deletes the device's auto-imported task (handled by backend signal).
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Badge,
  Button,
  Card,
  Dropdown,
  Input,
  Modal,
  Segmented,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  CloudDownloadOutlined,
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';

import DeviceFormModal from '../components/DeviceFormModal';
import { protocolsWithExcel } from '../protocols/registry';
import { apiClient } from '../services/apiClient';
import { fetchAllPages } from '../services/pagination';
import { downloadTemplate, listProtocols, type ProtocolDescriptor } from '../services/protocolApi';
import type { DeviceStatus } from '../services/deviceApi';

interface Device {
  id: number;
  site: number;
  name: string;
  code: string;
  protocol: string;
  ip_address: string;
  port: number | null;
  metadata: Record<string, unknown>;
  status?: DeviceStatus;
  created_at: string;
  updated_at: string;
}

/**
 * 状态列的文案与配色。只有在线/离线两态。
 *
 * 这一列以前是写死的绿色「在线」,不查任何数据 —— 一台从没连通过的设备也显示
 * 在线,比没有这一列还糟。现在的取值由后端从「连接告警 + 运行中的会话」推出来。
 *
 * 没有中间态:采集设计上永远在重试,所以「没在采」和「连不上」对操作员是同一
 * 件事 —— 这台设备现在拿不到数据,都该是红的。
 */
const DEVICE_STATUS: Record<
  DeviceStatus,
  { badge: 'success' | 'error'; label: string; hint: string }
> = {
  online: { badge: 'success', label: '在线', hint: '采集运行中,连接正常' },
  offline: { badge: 'error', label: '离线', hint: '拿不到数据 —— 连不上,或没有运行中的采集任务' },
};

const { Text, Title } = Typography;

const DeviceListPage = () => {
  const [devices, setDevices] = useState<Device[]>([]);
  const [protocols, setProtocols] = useState<ProtocolDescriptor[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [filterProtocol, setFilterProtocol] = useState<string>('all');
  const [modalOpen, setModalOpen] = useState(false);
  const [editingId, setEditingId] = useState<number | undefined>(undefined);
  const [defaultProtocol, setDefaultProtocol] = useState<string | undefined>();

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      // 标准 list 端点:DRF 全局分页后返回 `{ results }`;逐页合并取全量(XIU-9 / H10)。
      const devices = await fetchAllPages<Device>(async (limit, offset) => {
        const res = await apiClient.get('/config/devices/', {
          params: { limit, offset },
        });
        return res.data;
      });
      setDevices(devices);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    listProtocols().then(setProtocols).catch(() => undefined);
  }, [refresh]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return devices.filter((d) => {
      if (filterProtocol !== 'all' && d.protocol !== filterProtocol) return false;
      if (!q) return true;
      return (
        d.name.toLowerCase().includes(q) ||
        d.code.toLowerCase().includes(q) ||
        d.ip_address?.toLowerCase().includes(q) ||
        d.protocol.toLowerCase().includes(q)
      );
    });
  }, [devices, search, filterProtocol]);

  const handleDelete = (device: Device) => {
    Modal.confirm({
      title: '确定删除该设备?',
      content: `${device.name} (${device.code}) — 关联的自动导入任务会一起删除。`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        await apiClient.delete(`/config/devices/${device.id}/`);
        message.success('已删除');
        refresh();
      },
    });
  };

  const protocolByName = useMemo(() => {
    const map: Record<string, ProtocolDescriptor> = {};
    for (const p of protocols) map[p.name] = p;
    return map;
  }, [protocols]);

  const columns: ColumnsType<Device> = [
    {
      title: '设备',
      dataIndex: 'name',
      render: (name: string, row) => (
        <Link to={`/devices/${row.id}`}>
          <strong>{name}</strong>
          <div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {row.code}
            </Text>
          </div>
        </Link>
      ),
      sorter: (a, b) => a.name.localeCompare(b.name),
    },
    {
      title: '协议',
      dataIndex: 'protocol',
      render: (proto: string) => {
        const meta = protocolByName[proto];
        const color =
          meta?.category === 'industrial-ethernet' ? 'blue'
          : meta?.category === 'fieldbus' ? 'orange'
          : meta?.category === 'iot' ? 'purple'
          : meta?.category === 'opc' ? 'cyan'
          : 'default';
        return <Tag color={color}>{meta?.label ?? proto}</Tag>;
      },
      filters: protocols.map((p) => ({ text: p.label, value: p.name })),
      onFilter: (value, row) => row.protocol === value,
    },
    {
      title: '连接',
      render: (_v, row) => {
        const md = row.metadata || {};
        if (md.serial_port) {
          return (
            <Text style={{ fontFamily: 'monospace' }}>
              {String(md.serial_port)} @ {String(md.baudrate ?? '9600')}
            </Text>
          );
        }
        if (md.endpoint_url) {
          return <Text style={{ fontFamily: 'monospace' }}>{String(md.endpoint_url)}</Text>;
        }
        if (row.ip_address) {
          return (
            <Text style={{ fontFamily: 'monospace' }}>
              {row.ip_address}
              {row.port ? `:${row.port}` : ''}
            </Text>
          );
        }
        return <Text type="secondary">—</Text>;
      },
    },
    {
      title: '状态',
      width: 110,
      render: (_v, row) => {
        const meta = DEVICE_STATUS[row.status ?? 'offline'] ?? DEVICE_STATUS.offline;
        return (
          <Tooltip title={meta.hint}>
            <Badge status={meta.badge} text={meta.label} />
          </Tooltip>
        );
      },
    },
    {
      title: '操作',
      width: 200,
      align: 'right',
      render: (_v, row) => (
        <Space size="small">
          <Tooltip title="编辑">
            <Button
              icon={<EditOutlined />}
              size="small"
              onClick={() => {
                setEditingId(row.id);
                setDefaultProtocol(row.protocol);
                setModalOpen(true);
              }}
            />
          </Tooltip>
          <Tooltip title="测试连接">
            <Button
              icon={<ThunderboltOutlined />}
              size="small"
              onClick={async () => {
                try {
                  const res = await apiClient.post(`/config/devices/${row.id}/test-connection/`);
                  if (res.data.success) message.success(res.data.message ?? '连接正常');
                  else message.warning(res.data.message ?? '连接失败');
                } catch {
                  // interceptor already shown
                }
              }}
            />
          </Tooltip>
          <Tooltip title="删除">
            <Button
              icon={<DeleteOutlined />}
              size="small"
              danger
              onClick={() => handleDelete(row)}
            />
          </Tooltip>
        </Space>
      ),
    },
  ];

  const addMenu = useMemo(
    () => ({
      items: protocols.map((p) => ({
        key: p.name,
        label: (
          <span>
            {p.label}
            <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
              {p.category}
            </Text>
          </span>
        ),
      })),
      onClick: ({ key }: { key: string }) => {
        setEditingId(undefined);
        setDefaultProtocol(key);
        setModalOpen(true);
      },
    }),
    [protocols],
  );

  /**
   * 模板下拉:通用单表模板 + 各协议在注册表里声明的专属模板。
   *
   * 通用模板是一张 40 列的大宽表,scada 用它得逐行重复 broker/账号/密码 ——
   * 那正是网关模型要消掉的重复,所以 scada 必须给出自己的两表模板。
   */
  const templateMenu = useMemo(
    () => ({
      items: [
        { key: 'generic', label: '通用模板(全部协议 · 单表)' },
        ...protocolsWithExcel().map((e) => ({ key: e.protocol, label: e.label })),
      ],
      onClick: ({ key }: { key: string }) => {
        const custom = protocolsWithExcel().find((e) => e.protocol === key);
        (custom ? custom.download() : downloadTemplate()).catch(() => undefined);
      },
    }),
    [],
  );

  const segOptions = useMemo(
    () => [
      { label: `全部 (${devices.length})`, value: 'all' },
      ...protocols.map((p) => ({
        label: `${p.label} (${devices.filter((d) => d.protocol === p.name).length})`,
        value: p.name,
      })),
    ],
    [devices, protocols],
  );

  return (
    <div style={{ padding: 24 }}>
      <Card variant="borderless" style={{ marginBottom: 16 }}>
        <Space style={{ width: '100%', justifyContent: 'space-between', flexWrap: 'wrap' }}>
          <div>
            <Title level={3} style={{ margin: 0 }}>
              设备管理
            </Title>
            <Text type="secondary">管理与监控所有连接的工业设备</Text>
          </div>
          <Space wrap>
            <Button icon={<ReloadOutlined />} onClick={refresh}>
              刷新
            </Button>
            <Dropdown menu={templateMenu} trigger={['click']}>
              <Button icon={<CloudDownloadOutlined />}>下载 Excel 模板</Button>
            </Dropdown>
            <Dropdown menu={addMenu} trigger={['click']}>
              <Button type="primary" icon={<PlusOutlined />}>
                添加设备
              </Button>
            </Dropdown>
          </Space>
        </Space>
      </Card>

      <Card variant="borderless">
        <Space style={{ marginBottom: 16, width: '100%', justifyContent: 'space-between', flexWrap: 'wrap' }}>
          <Input.Search
            placeholder="搜索设备名称、编码、IP 或协议"
            allowClear
            style={{ width: 360 }}
            onChange={(e) => setSearch(e.target.value)}
          />
          <Segmented
            value={filterProtocol}
            onChange={(v) => setFilterProtocol(String(v))}
            options={segOptions}
          />
        </Space>

        <Table<Device>
          rowKey="id"
          columns={columns}
          dataSource={filtered}
          loading={loading}
          locale={{ emptyText: '暂无设备 — 请通过"添加设备"或"导入"创建' }}
          pagination={{ pageSize: 10, showSizeChanger: true }}
        />
      </Card>

      <DeviceFormModal
        open={modalOpen}
        deviceId={editingId}
        defaultProtocol={defaultProtocol}
        onClose={() => setModalOpen(false)}
        onSaved={refresh}
      />
    </div>
  );
};

export default DeviceListPage;
