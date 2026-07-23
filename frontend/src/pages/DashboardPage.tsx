/**
 * 任务总览。
 *
 * 这一版是系统级整改的产物:删掉了原来一堆恒 0、误导、或点了没反应的东西 ——
 * 「成功率」(前端 status key 和后端对不上、TaskRun 也没有终态,永远 0)、
 * 「异常告警」卡(取 status.error 恒 0,与告警中心口径冲突)、假的成功/异常
 * 环形图、以及四个死控件(时间范围切换/类型下拉/导出/查看全部,全都不接任何
 * 数据)。只留真实、有价值的:任务数、设备在线、任务列表、最近运行、快捷入口。
 *
 * 设备「在线」直接消费后端 device.status(online/offline 两态),与设备管理页
 * 同源同口径 —— 不再自己从会话 device_health 重算三态,避免两页对不上。
 */
import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Card,
  Col,
  Empty,
  Row,
  Statistic,
  Table,
  Tag,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  ApiOutlined,
  CloudUploadOutlined,
  DatabaseOutlined,
  HistoryOutlined,
  LineChartOutlined,
  PlayCircleOutlined,
  RightOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';

import { isAbortError } from '../services/http';
import { fetchAllPages, withLimitOffset } from '../services/pagination';

const { Title, Text } = Typography;

interface TaskRun {
  task: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  worker: string | null;
  log_reference: string | null;
}

interface OverviewPayload {
  total_tasks: number;
  active_tasks: number;
  status: Record<string, number>;
  recent_runs: TaskRun[];
  generated_at: string;
}

interface TaskItem {
  id: number;
  code: string;
  name: string;
  is_active: boolean;
}

/** 运行记录状态 → 中文 + 颜色。原来直接渲染英文原值且 succeeded 落灰。 */
const RUN_STATUS: Record<string, { text: string; color: string }> = {
  running: { text: '运行中', color: 'processing' },
  succeeded: { text: '成功', color: 'success' },
  completed: { text: '完成', color: 'success' },
  stopped: { text: '已停止', color: 'default' },
  failed: { text: '失败', color: 'error' },
  error: { text: '错误', color: 'error' },
};

const DashboardPage = () => {
  const [data, setData] = useState<OverviewPayload | null>(null);
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [deviceStats, setDeviceStats] = useState({ total: 0, online: 0 });
  const [runningCount, setRunningCount] = useState(0);
  const [initialLoading, setInitialLoading] = useState(true);
  const [, setError] = useState<string | null>(null);

  const fetchOverview = useCallback(async (signal?: AbortSignal) => {
    const response = await fetch('/api/config/tasks/overview/?site_code=default', { signal });
    if (!response.ok) throw new Error(await response.text());
    return (await response.json()) as OverviewPayload;
  }, []);

  const fetchTasks = useCallback(async (signal?: AbortSignal) => {
    return fetchAllPages<TaskItem>(async (limit, offset) => {
      const response = await fetch(
        withLimitOffset('/api/config/tasks/?site_code=default', limit, offset),
        { signal },
      );
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    });
  }, []);

  // 设备在线口径:直接用后端 device.status(两态),与设备管理页一致。
  const fetchDevices = useCallback(async (signal?: AbortSignal) => {
    return fetchAllPages<{ id: number; status?: string }>(async (limit, offset) => {
      const response = await fetch(withLimitOffset('/api/config/devices/', limit, offset), { signal });
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    });
  }, []);

  const fetchActiveSessions = useCallback(async (signal?: AbortSignal) => {
    const response = await fetch('/api/acquisition/sessions/active/', { signal });
    if (!response.ok) throw new Error(await response.text());
    return (await response.json()) as Array<{ id: number; status: string }>;
  }, []);

  useEffect(() => {
    const aborter = new AbortController();
    const { signal } = aborter;

    const load = async () => {
      try {
        // allSettled:单个接口失败不该让整页空白 —— 渲染成功的部分,其余标注。
        const [overviewRes, tasksRes, devicesRes, sessionsRes] = await Promise.allSettled([
          fetchOverview(signal),
          fetchTasks(signal),
          fetchDevices(signal),
          fetchActiveSessions(signal),
        ]);
        if (signal.aborted) return;

        const failed: string[] = [];
        if (overviewRes.status === 'fulfilled') setData(overviewRes.value);
        else failed.push('任务概览');
        if (tasksRes.status === 'fulfilled') setTasks(tasksRes.value);
        else failed.push('任务列表');

        const devices = devicesRes.status === 'fulfilled' ? devicesRes.value : [];
        if (devicesRes.status === 'rejected') failed.push('设备');
        setDeviceStats({
          total: devices.length,
          online: devices.filter((d) => d.status === 'online').length,
        });

        const sessions = sessionsRes.status === 'fulfilled' ? sessionsRes.value : [];
        if (sessionsRes.status === 'rejected') failed.push('活跃会话');
        setRunningCount(sessions.filter((s) => s.status === 'running').length);

        setError(failed.length > 0 ? `部分数据加载失败：${failed.join('、')}` : null);
      } catch (err) {
        if (!isAbortError(err)) setError((err as Error).message);
      } finally {
        if (!signal.aborted) setInitialLoading(false);
      }
    };

    load();
    const interval = setInterval(load, 30000);
    return () => {
      aborter.abort();
      clearInterval(interval);
    };
  }, [fetchOverview, fetchTasks, fetchDevices, fetchActiveSessions]);

  const formatTime = (date: string | null) => {
    if (!date) return '-';
    return new Date(date).toLocaleString('zh-CN', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  const taskColumns: ColumnsType<TaskItem> = [
    {
      title: '任务编码',
      dataIndex: 'code',
      render: (code: string) => <Text style={{ fontFamily: 'monospace' }}>{code}</Text>,
    },
    { title: '名称', dataIndex: 'name' },
    {
      title: '状态',
      dataIndex: 'is_active',
      width: 90,
      render: (active: boolean) => (
        <Tag color={active ? 'success' : 'default'}>{active ? '启用' : '停用'}</Tag>
      ),
    },
    {
      title: '',
      width: 80,
      render: () => (
        <Link to="/acquisition">
          <PlayCircleOutlined /> 控制
        </Link>
      ),
    },
  ];

  const runColumns: ColumnsType<TaskRun> = [
    {
      title: '任务',
      dataIndex: 'task',
      render: (task: string, r) => (
        <div>
          <div>{task}</div>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {r.worker || '本地'}
          </Text>
        </div>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 100,
      render: (s: string) => {
        const meta = RUN_STATUS[s?.toLowerCase()] ?? { text: s, color: 'default' };
        return <Tag color={meta.color}>{meta.text}</Tag>;
      },
    },
    {
      title: '时间',
      dataIndex: 'started_at',
      width: 130,
      render: (t: string | null) => <Text type="secondary">{formatTime(t)}</Text>,
    },
  ];

  const quickActions = [
    { to: '/acquisition', icon: <PlayCircleOutlined />, title: '采集控制', desc: '启停任务、管理测点' },
    { to: '/devices', icon: <DatabaseOutlined />, title: '设备管理', desc: '设备与测点配置' },
    { to: '/import', icon: <CloudUploadOutlined />, title: '导入配置', desc: '上传 Excel 配置' },
    { to: '/data', icon: <LineChartOutlined />, title: '数据可视化', desc: '历史趋势查询' },
    { to: '/alarms', icon: <ThunderboltOutlined />, title: '告警中心', desc: '连接/系统告警' },
    { to: '/versions', icon: <HistoryOutlined />, title: '版本历史', desc: '配置版本记录' },
  ];

  return (
    <div style={{ padding: 24 }}>
      <div style={{ marginBottom: 20 }}>
        <Title level={3} style={{ margin: 0 }}>
          数据采集平台
        </Title>
        <Text type="secondary">实时监控和管理 IoT 设备数据采集</Text>
      </div>

      {/* 真实指标 */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={12} md={6}>
          <Card variant="borderless" loading={initialLoading}>
            <Statistic
              title="任务总数"
              value={data?.total_tasks ?? 0}
              prefix={<DatabaseOutlined />}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card variant="borderless" loading={initialLoading}>
            <Statistic title="启用任务" value={data?.active_tasks ?? 0} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card variant="borderless" loading={initialLoading}>
            <Statistic
              title="运行中会话"
              value={runningCount}
              valueStyle={{ color: runningCount > 0 ? '#52c41a' : undefined }}
              prefix={<PlayCircleOutlined />}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card variant="borderless" loading={initialLoading}>
            <Statistic
              title="设备在线"
              value={deviceStats.online}
              suffix={`/ ${deviceStats.total}`}
              prefix={<ApiOutlined />}
              valueStyle={{
                color: deviceStats.total > 0 && deviceStats.online === 0 ? '#ff4d4f' : undefined,
              }}
            />
          </Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={24} lg={12}>
          <Card
            variant="borderless"
            title="任务列表"
            extra={
              <Link to="/acquisition">
                采集控制 <RightOutlined />
              </Link>
            }
          >
            {tasks.length === 0 ? (
              <Empty description="暂无任务 —— 导入配置或在采集控制页新建" />
            ) : (
              <Table<TaskItem>
                rowKey="id"
                size="small"
                columns={taskColumns}
                dataSource={tasks.slice(0, 6)}
                pagination={false}
              />
            )}
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card variant="borderless" title="最近运行">
            {!data?.recent_runs || data.recent_runs.length === 0 ? (
              <Empty description="暂无运行记录 —— 启动采集任务后在此显示" />
            ) : (
              <Table<TaskRun>
                rowKey={(r) => r.log_reference || `${r.task}|${r.started_at || ''}`}
                size="small"
                columns={runColumns}
                dataSource={data.recent_runs.slice(0, 6)}
                pagination={false}
              />
            )}
          </Card>
        </Col>
      </Row>

      <Card variant="borderless" title="快速操作">
        <Row gutter={[16, 16]}>
          {quickActions.map((a) => (
            <Col xs={12} md={8} lg={4} key={a.to}>
              <Link to={a.to}>
                <Card size="small" hoverable style={{ textAlign: 'center' }}>
                  <div style={{ fontSize: 22, marginBottom: 6 }}>{a.icon}</div>
                  <div style={{ fontWeight: 600 }}>{a.title}</div>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {a.desc}
                  </Text>
                </Card>
              </Link>
            </Col>
          ))}
        </Row>
      </Card>
    </div>
  );
};

export default DashboardPage;
