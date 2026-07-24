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
  CloudUploadOutlined,
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';

import ConnectionTestModal from '../components/ConnectionTestModal';
import DeviceFormModal from '../components/DeviceFormModal';
import ProtocolExcelImportModal from '../components/ProtocolExcelImportModal';
import { protocolsWithExcel } from '../protocols/registry';
import { apiClient } from '../services/apiClient';
import { fetchAllPages } from '../services/pagination';
import {
  downloadDeviceExport,
  downloadProtocolTemplateV2,
  downloadTemplate,
  exportProtocolDevices,
  listProtocols,
  protocolTagColor,
  type ProtocolDescriptor,
} from '../services/protocolApi';
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

/**
 * v2 每协议两表 Excel(docs/excel-import-export-v2.md)不覆盖这两个协议:
 * scada 有自己的网关两表流程(protocolsWithExcel() 已经把它列进模板下拉),
 * simulator 不是生产协议。模板下拉的 v2 分组、导出联动都要排除它俩。
 */
const V2_EXCLUDED_PROTOCOLS = new Set(['scada', 'simulator']);

const DeviceListPage = () => {
  const [devices, setDevices] = useState<Device[]>([]);
  const [protocols, setProtocols] = useState<ProtocolDescriptor[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [filterProtocol, setFilterProtocol] = useState<string>('all');
  const [modalOpen, setModalOpen] = useState(false);
  const [importModalOpen, setImportModalOpen] = useState(false);
  const [editingId, setEditingId] = useState<number | undefined>(undefined);
  const [defaultProtocol, setDefaultProtocol] = useState<string | undefined>();
  // 「测试连接」立刻开弹窗展示过程,而不是等几秒蹦个 toast。
  const [testing, setTesting] = useState<Device | undefined>();

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
      content: `${device.name} (${device.code}) —— 测点和自动导入的任务都会一并删除,不影响设备本身之外的东西。`,
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
        return <Tag color={protocolTagColor(meta?.category)}>{meta?.label ?? proto}</Tag>;
      },
      // 协议筛选只在顶部 Segmented 做一次(带每协议计数)。这里以前还挂了一套
      // filters/onFilter,两套筛选叠加会导致"选 A 协议 + 勾选 B 协议列筛选"
      // 结果恒为空且无从判断,故移除(rank17)。
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
              onClick={() => setTesting(row)}
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

  /** 生产协议(排除 scada/simulator)——模板下拉的 v2 分组来源,动态自协议清单。 */
  const v2Protocols = useMemo(
    () => protocols.filter((p) => !V2_EXCLUDED_PROTOCOLS.has(p.name)),
    [protocols],
  );

  /**
   * 模板下拉重组(docs/excel-import-export-v2.md「前端」节):
   *   1. 每个生产协议一项,走 v2 每协议两表模板(设备+测点);
   *   2. scada 项保持指向既有网关两表模板(protocolsWithExcel() 里登记的那份);
   *   3. 通用单表模板沉底并标注 legacy —— 40 列大宽表,只有跨协议批量场景还用得上。
   *
   * 「导出当前设备」联动当前协议筛选(Segmented 选中的 filterProtocol):
   *   - 具体生产协议 → v2 per-protocol 导出;
   *   - 「全部」→ 仍是 40 列全量导出(legacy,跨协议只有大宽表装得下,菜单文案标注);
   *   - scada → 提示去网关页导出(连接参数挂在网关而非 device.metadata,两种格式都装不下)。
   */
  const templateMenu = useMemo(
    () => ({
      items: [
        ...v2Protocols.map((p) => ({
          key: `v2:${p.name}`,
          label: `${p.label} 模板(设备+测点两表)`,
        })),
        ...protocolsWithExcel().map((e) => ({ key: `custom:${e.protocol}`, label: e.label })),
        { type: 'divider' as const },
        { key: 'generic', label: '通用单表模板(legacy · 全部协议)' },
        { type: 'divider' as const },
        {
          key: 'export-current-devices',
          label:
            filterProtocol === 'all'
              ? '导出当前设备(Excel · legacy 全量单表)'
              : '导出当前设备(Excel)',
        },
      ],
      onClick: ({ key }: { key: string }) => {
        if (key === 'export-current-devices') {
          if (filterProtocol === 'scada') {
            message.warning('SCADA 设备请在「SCADA 网关」页导出(网关两表格式)。');
            return;
          }
          if (filterProtocol === 'all' || V2_EXCLUDED_PROTOCOLS.has(filterProtocol)) {
            const protocols = filterProtocol === 'all' ? undefined : [filterProtocol];
            downloadDeviceExport(protocols).catch(() => undefined);
            return;
          }
          exportProtocolDevices(filterProtocol).catch(() => undefined);
          return;
        }
        if (key === 'generic') {
          downloadTemplate().catch(() => undefined);
          return;
        }
        if (key.startsWith('v2:')) {
          downloadProtocolTemplateV2(key.slice('v2:'.length)).catch(() => undefined);
          return;
        }
        if (key.startsWith('custom:')) {
          const protocol = key.slice('custom:'.length);
          const custom = protocolsWithExcel().find((e) => e.protocol === protocol);
          custom?.download().catch(() => undefined);
        }
      },
    }),
    [filterProtocol, v2Protocols],
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
            <Button icon={<CloudUploadOutlined />} onClick={() => setImportModalOpen(true)}>
              导入配置
            </Button>
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

      <ConnectionTestModal
        open={Boolean(testing)}
        deviceId={testing?.id}
        deviceName={testing?.name}
        onClose={() => setTesting(undefined)}
      />

      <DeviceFormModal
        open={modalOpen}
        deviceId={editingId}
        defaultProtocol={defaultProtocol}
        onClose={() => setModalOpen(false)}
        onSaved={refresh}
      />

      <ProtocolExcelImportModal
        open={importModalOpen}
        onClose={() => setImportModalOpen(false)}
        onImported={refresh}
      />
    </div>
  );
};

export default DeviceListPage;
