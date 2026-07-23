import { useEffect, useState } from 'react';
import { useParams, useNavigate, Link } from 'react-router-dom';
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Input,
  Row,
  Spin,
  Statistic,
  Table,
  Tag,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  ArrowLeftOutlined,
  ApiOutlined,
  DownloadOutlined,
  EditOutlined,
  DeleteOutlined,
  PlayCircleOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import {
  fetchDevice,
  fetchDevicePoints,
  fetchDeviceStats,
  deleteDevice,
  Device,
  Point,
  DeviceStats,
} from '../services/deviceApi';
import { listProtocols, protocolTagColor, type ProtocolDescriptor } from '../services/protocolApi';
import ConnectionTestModal from '../components/ConnectionTestModal';
import DeviceFormModal from '../components/DeviceFormModal';

const { Title, Text } = Typography;

type RelatedTask = DeviceStats['related_tasks'][number];

const DeviceDetailPage = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const deviceId = parseInt(id || '0', 10);
  // antd5:静态 message/Modal.confirm 拿不到 ConfigProvider 的自定义 theme,
  // 会在控制台刷 "Static function can not consume context" 警告 —— 改用
  // App.useApp() 拿 context-aware 的实例(App.tsx 的 <AntdApp> 已经包了)。
  const { message, modal } = App.useApp();

  const [device, setDevice] = useState<Device | null>(null);
  const [points, setPoints] = useState<Point[]>([]);
  const [stats, setStats] = useState<DeviceStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  // 「修改配置」走的是和设备管理页同一个弹窗:按协议分发到各自的配置界面。
  const [editOpen, setEditOpen] = useState(false);
  // 「测试连接」立刻开弹窗展示整个过程,不再是按下去干等几秒再出结果。
  const [testOpen, setTestOpen] = useState(false);
  // 协议展示名/分类色,与设备列表页同源(listProtocols),不再直接渲染机器名。
  const [protocols, setProtocols] = useState<ProtocolDescriptor[]>([]);

  useEffect(() => {
    loadDeviceData();
    // 仅在 deviceId 变化时重新加载；loadDeviceData 每次渲染重建，不入依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceId]);

  useEffect(() => {
    listProtocols().then(setProtocols).catch(() => undefined);
  }, []);

  const loadDeviceData = async () => {
    setLoading(true);
    setError(null);

    try {
      const [deviceData, pointsData, statsData] = await Promise.all([
        fetchDevice(deviceId),
        fetchDevicePoints(deviceId),
        fetchDeviceStats(deviceId),
      ]);

      setDevice(deviceData);
      setPoints(pointsData);
      setStats(statsData);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };


  const handleDelete = () => {
    if (!device) return;
    // 与设备列表页(DeviceListPage)口径一致:测点和自动导入的任务都会一并
    // 删除,不影响设备本身之外的东西。以前这里一个 confirm 只提测点、列表页
    // 只提任务,两处文案互相矛盾。
    modal.confirm({
      title: '确定删除该设备?',
      content: `${device.name} (${device.code}) —— 测点和自动导入的任务都会一并删除,不影响设备本身之外的东西。`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          await deleteDevice(deviceId);
          message.success('已删除');
          navigate('/devices');
        } catch (err) {
          message.error(`删除失败: ${(err as Error).message}`);
        }
      },
    });
  };

  const handleExportCSV = () => {
    if (points.length === 0) {
      message.warning('没有测点数据可导出');
      return;
    }

    // 不含 Kafka 列:to_kafka 只在这个页面出现过,设不了也用不上,和
    // InfluxDB 采集链路无关(rank15b)。采样率标注为"继承任务"—— 真正生效
    // 的是任务级 task.sample_rate_hz,这里每测点的值是死值,容易误导。
    const headers = ['编码', '地址', '描述', '采样率(Hz,继承任务)'];
    const rows = points.map(p => [
      p.code,
      p.address,
      p.description,
      p.sample_rate_hz,
    ]);

    const csvContent = [
      headers.join(','),
      ...rows.map(row => row.join(',')),
    ].join('\n');

    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `device_${device?.code}_points.csv`;
    link.click();
  };

  const filteredPoints = points.filter(point =>
    point.code.toLowerCase().includes(searchTerm.toLowerCase()) ||
    point.description.toLowerCase().includes(searchTerm.toLowerCase()) ||
    point.address.toLowerCase().includes(searchTerm.toLowerCase())
  );

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '60vh' }}>
        <Spin size="large" />
      </div>
    );
  }

  if (error) {
    return <Alert type="error" showIcon message="加载失败" description={error} />;
  }

  if (!device) {
    return <Empty description="设备不存在" style={{ marginTop: 80 }} />;
  }

  const taskColumns: ColumnsType<RelatedTask> = [
    { title: '任务编码', dataIndex: 'code' },
    { title: '任务名称', dataIndex: 'name' },
    {
      title: '状态',
      dataIndex: 'is_active',
      render: (active: boolean) => <Tag color={active ? 'success' : 'default'}>{active ? '启用' : '停用'}</Tag>,
    },
    {
      title: '操作',
      render: () => (
        <Link to="/acquisition">
          <PlayCircleOutlined /> 控制
        </Link>
      ),
    },
  ];

  const pointColumns: ColumnsType<Point> = [
    { title: '编码', dataIndex: 'code', render: (v: string) => <Text strong>{v}</Text> },
    { title: '地址', dataIndex: 'address', render: (v: string) => <Text type="secondary">{v}</Text> },
    { title: '描述', dataIndex: 'description', render: (v: string) => <Text type="secondary">{v}</Text> },
    {
      // 真正生效的是任务级 task.sample_rate_hz;每测点这个值是死值,标注清楚
      // 以免误导(rank15c)。Kafka 列已整体移除:to_kafka 只在这个页面出现过,
      // 设不了也与 InfluxDB 采集链路无关。
      title: '采样率 (Hz,继承任务)',
      dataIndex: 'sample_rate_hz',
    },
  ];

  return (
    <div style={{ maxWidth: 1200 }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 24, gap: 16, flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <Link to="/devices" style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <ArrowLeftOutlined /> 返回设备列表
          </Link>
          <Title level={3} style={{ margin: 0 }}>{device.name}</Title>
          <Tag color={protocolTagColor(protocols.find((p) => p.name === device.protocol)?.category)}>
            {protocols.find((p) => p.name === device.protocol)?.label ?? device.protocol}
          </Tag>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <Button icon={<EditOutlined />} onClick={() => setEditOpen(true)}>修改配置</Button>
          <Button icon={<ApiOutlined />} onClick={() => setTestOpen(true)}>测试连接</Button>
          <Button danger icon={<DeleteOutlined />} onClick={handleDelete}>删除设备</Button>
        </div>
      </div>

      {/* Info Grid */}
      <Row gutter={20} style={{ marginBottom: 24 }}>
        <Col xs={24} md={12}>
          <Card title="基本信息" size="small" style={{ height: '100%' }}>
            <Descriptions column={1} size="small">
              <Descriptions.Item label="设备编码">{device.code}</Descriptions.Item>
              <Descriptions.Item label="IP 地址">{device.ip_address || 'N/A'}</Descriptions.Item>
              <Descriptions.Item label="端口">{device.port || 'N/A'}</Descriptions.Item>
              <Descriptions.Item label="站点 ID">{device.site}</Descriptions.Item>
            </Descriptions>
          </Card>
        </Col>

        {stats && (
          <Col xs={24} md={12}>
            <Card title="统计信息" size="small" style={{ height: '100%' }}>
              <Row gutter={16}>
                <Col span={12}>
                  <Statistic title="测点总数" value={stats.total_points} />
                </Col>
                <Col span={12}>
                  <Statistic title="关联任务" value={stats.task_count} />
                </Col>
              </Row>
              <div style={{ marginTop: 16 }}>
                <Text type="secondary" style={{ fontSize: 12 }}>最近采集</Text>
                <div style={{ fontSize: 14, fontWeight: 500 }}>
                  {stats.last_acquisition ? new Date(stats.last_acquisition).toLocaleString('zh-CN') : '从未采集'}
                </div>
              </div>
            </Card>
          </Col>
        )}
      </Row>

      {/* Related Tasks */}
      {stats && stats.related_tasks.length > 0 && (
        <div style={{ marginBottom: 24 }}>
          <Title level={4} style={{ marginBottom: 16 }}>关联任务 ({stats.task_count})</Title>
          <Table<RelatedTask>
            rowKey="id"
            size="small"
            columns={taskColumns}
            dataSource={stats.related_tasks.slice(0, 5)}
            pagination={false}
          />
        </div>
      )}

      {/* Points List */}
      <div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16, gap: 12, flexWrap: 'wrap' }}>
          <Title level={4} style={{ margin: 0 }}>测点列表 ({filteredPoints.length} / {points.length})</Title>
          <div style={{ display: 'flex', gap: 8 }}>
            <Input
              prefix={<SearchOutlined />}
              placeholder="搜索测点..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              style={{ width: 200 }}
              allowClear
            />
            <Button icon={<DownloadOutlined />} onClick={handleExportCSV}>导出 CSV</Button>
          </div>
        </div>

        {filteredPoints.length === 0 ? (
          <Empty description={searchTerm ? '没有匹配的测点' : '暂无测点数据'} />
        ) : (
          <Table<Point>
            rowKey="id"
            size="small"
            columns={pointColumns}
            dataSource={filteredPoints}
            pagination={false}
          />
        )}
      </div>

      <ConnectionTestModal
        open={testOpen}
        deviceId={deviceId}
        deviceName={device.name}
        onClose={() => setTestOpen(false)}
      />

      <DeviceFormModal
        open={editOpen}
        deviceId={deviceId}
        defaultProtocol={device.protocol}
        onClose={() => setEditOpen(false)}
        onSaved={loadDeviceData}
      />
    </div>
  );
};

export default DeviceDetailPage;
