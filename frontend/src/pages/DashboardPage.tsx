import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { isAbortError } from '../services/http';
import { fetchAllPages, withLimitOffset } from '../services/pagination';
import './DashboardPage.css';

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

interface DeviceStats {
  total: number;
  online: number;
  offline: number;
  error: number;
}

const DashboardPage = () => {
  const [data, setData] = useState<OverviewPayload | null>(null);
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [deviceStats, setDeviceStats] = useState<DeviceStats>({ total: 0, online: 0, offline: 0, error: 0 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [timeRange, setTimeRange] = useState<'24h' | '7d' | '30d'>('24h');

  const fetchOverview = useCallback(async (signal?: AbortSignal) => {
    const response = await fetch('/api/config/tasks/overview/?site_code=default', { signal });
    if (!response.ok) throw new Error(await response.text());
    return (await response.json()) as OverviewPayload;
  }, []);

  // 标准 list 端点:DRF 全局分页后返回 `{ results }`;逐页合并仍取全量(XIU-9 / H10)。
  const fetchTasks = useCallback(async (signal?: AbortSignal) => {
    return fetchAllPages<TaskItem>(async (limit, offset) => {
      const response = await fetch(
        withLimitOffset('/api/config/tasks/?site_code=default', limit, offset),
        { signal }
      );
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    });
  }, []);

  const fetchDevices = useCallback(async (signal?: AbortSignal) => {
    return fetchAllPages<{ id: number; protocol: string }>(async (limit, offset) => {
      const response = await fetch(
        withLimitOffset('/api/config/devices/', limit, offset),
        { signal }
      );
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    });
  }, []);

  const fetchActiveSessions = useCallback(async (signal?: AbortSignal) => {
    const response = await fetch('/api/acquisition/sessions/active/', { signal });
    if (!response.ok) throw new Error(await response.text());
    return (await response.json()) as Array<{
      id: number;
      task: number;
      status: string;
      metadata?: { device_health?: Record<string, { status: string }> };
    }>;
  }, []);

  useEffect(() => {
    const aborter = new AbortController();
    const { signal } = aborter;

    const load = async () => {
      setLoading(true);
      try {
        // allSettled: a single failing endpoint must not blank the whole
        // dashboard — render whatever succeeded and flag the rest.
        const [overviewRes, tasksRes, devicesRes, sessionsRes] =
          await Promise.allSettled([
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

        const activeSessions =
          sessionsRes.status === 'fulfilled' ? sessionsRes.value : [];
        if (sessionsRes.status === 'rejected') failed.push('活跃会话');

        // Real device counts: a device is "online" if it appears in any
        // running session's device_health with status === "healthy".
        const healthyCodes = new Set<string>();
        const errorCodes = new Set<string>();
        for (const session of activeSessions) {
          const dh = session.metadata?.device_health || {};
          for (const [code, info] of Object.entries(dh)) {
            if (info.status === 'healthy') healthyCodes.add(code);
            else errorCodes.add(code);
          }
        }
        const total = devices.length;
        const online = healthyCodes.size;
        const errored = errorCodes.size;
        setDeviceStats({
          total,
          online,
          error: errored,
          offline: Math.max(0, total - online - errored),
        });

        setError(
          failed.length > 0 ? `部分数据加载失败：${failed.join('、')}` : null,
        );
      } catch (err) {
        if (!isAbortError(err)) setError((err as Error).message);
      } finally {
        if (!signal.aborted) setLoading(false);
      }
    };

    load();
    // 30s auto-refresh keeps the dashboard meaningful without WebSocket plumbing
    const interval = setInterval(load, 30000);
    return () => {
      aborter.abort();
      clearInterval(interval);
    };
  }, [fetchOverview, fetchTasks, fetchDevices, fetchActiveSessions]);

  const getStatusBadgeClass = (status: string) => {
    switch (status.toLowerCase()) {
      case 'running':
      case 'active':
        return 'status-badge status-badge--running';
      case 'success':
      case 'completed':
        return 'status-badge status-badge--success';
      case 'error':
      case 'failed':
        return 'status-badge status-badge--error';
      default:
        return 'status-badge status-badge--stopped';
    }
  };

  const formatDuration = (started: string | null, finished: string | null) => {
    if (!started || !finished) return '-';
    const diff = new Date(finished).getTime() - new Date(started).getTime();
    const seconds = Math.floor(diff / 1000);
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
    const hours = Math.floor(minutes / 60);
    return `${hours}h ${minutes % 60}m`;
  };

  const formatTime = (date: string | null) => {
    if (!date) return '-';
    return new Date(date).toLocaleString('zh-CN', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  const totalStatusCount = Object.values(data?.status || {}).reduce((a, b) => a + b, 0);
  const successRate = totalStatusCount > 0
    ? Math.round(((data?.status?.success || 0) + (data?.status?.completed || 0)) / totalStatusCount * 100)
    : 0;

  if (loading) {
    return (
      <div className="dashboard">
        <div className="dashboard__loading">
          <div className="loading">
            <div className="loading__spinner" />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="dashboard">
      {/* Page Header */}
      <div className="dashboard__header">
        <div className="dashboard__header-left">
          <h1 className="dashboard__title">数据采集平台</h1>
          <p className="dashboard__subtitle">实时监控和管理您的IoT设备数据采集</p>
        </div>
        <div className="dashboard__header-right">
          <div className="time-range-selector">
            <button
              className={`time-range-btn ${timeRange === '24h' ? 'time-range-btn--active' : ''}`}
              onClick={() => setTimeRange('24h')}
            >
              24小时
            </button>
            <button
              className={`time-range-btn ${timeRange === '7d' ? 'time-range-btn--active' : ''}`}
              onClick={() => setTimeRange('7d')}
            >
              7天
            </button>
            <button
              className={`time-range-btn ${timeRange === '30d' ? 'time-range-btn--active' : ''}`}
              onClick={() => setTimeRange('30d')}
            >
              30天
            </button>
          </div>
        </div>
      </div>

      {/* Metrics Row */}
      <div className="metrics-row stagger">
        <div className="metric-card animate-slideIn">
          <div className="metric-card__glow" />
          <div className="metric-card__icon metric-card__icon--primary">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <rect x="3" y="3" width="7" height="7" rx="1" />
              <rect x="14" y="3" width="7" height="7" rx="1" />
              <rect x="3" y="14" width="7" height="7" rx="1" />
              <rect x="14" y="14" width="7" height="7" rx="1" />
            </svg>
          </div>
          <div className="metric-card__value">{data?.total_tasks || 0}</div>
          <div className="metric-card__label">
            任务总数
          </div>
        </div>

        <div className="metric-card animate-slideIn">
          <div className="metric-card__glow" />
          <div className="metric-card__icon metric-card__icon--success">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
              <polyline points="22 4 12 14.01 9 11.01" />
            </svg>
          </div>
          <div className="metric-card__value">{data?.active_tasks || 0}</div>
          <div className="metric-card__label">
            启用任务
            <span className="metric-card__badge">活跃</span>
          </div>
        </div>

        <div className="metric-card animate-slideIn">
          <div className="metric-card__glow" />
          <div className="metric-card__icon metric-card__icon--info">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <rect x="2" y="3" width="20" height="14" rx="2" />
              <line x1="8" y1="21" x2="16" y2="21" />
              <line x1="12" y1="17" x2="12" y2="21" />
            </svg>
          </div>
          <div className="metric-card__value">{deviceStats.total}</div>
          <div className="metric-card__label">
            设备总数
            <span className="metric-card__badge metric-card__trend--up">
              在线 {deviceStats.online}
            </span>
          </div>
        </div>

        <div className="metric-card animate-slideIn">
          <div className="metric-card__glow" />
          <div className="metric-card__icon metric-card__icon--warning">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
              <line x1="12" y1="9" x2="12" y2="13" />
              <line x1="12" y1="17" x2="12.01" y2="17" />
            </svg>
          </div>
          <div className="metric-card__value">{deviceStats.error}</div>
          <div className="metric-card__label">
            异常设备
            {deviceStats.error > 0 && <span className="metric-card__badge metric-card__badge--warning">需要关注</span>}
          </div>
        </div>
      </div>

      {/* Charts Row */}
      <div className="charts-grid">
        {/* Status Distribution Chart */}
        <div className="chart-card animate-scaleIn">
          <div className="chart-card__header">
            <div>
              <h3 className="chart-card__title">状态分布</h3>
              <p className="chart-card__subtitle">任务运行状态概览</p>
            </div>
            <div className="chart-card__action">
              <select className="form-select form-select--sm">
                <option>全部类型</option>
                <option>Modbus</option>
                <option>MQTT</option>
              </select>
            </div>
          </div>
          <div className="chart-card__body">
            <div className="chart-content">
              <div className="donut-chart">
                <svg viewBox="0 0 100 100" className="donut-chart__svg">
                  {/* Success segment */}
                  <circle
                    cx="50"
                    cy="50"
                    r="40"
                    fill="none"
                    stroke="var(--success)"
                    strokeWidth="12"
                    strokeDasharray={`${successRate * 2.51} 251`}
                    transform="rotate(-90 50 50)"
                    className="donut-chart__segment"
                  />
                  {/* Error segment */}
                  <circle
                    cx="50"
                    cy="50"
                    r="40"
                    fill="none"
                    stroke="var(--error)"
                    strokeWidth="12"
                    strokeDasharray={`${((data?.status?.error || 0) / (totalStatusCount || 1)) * 251} 251`}
                    strokeDashoffset={`-${successRate * 2.51}`}
                    transform="rotate(-90 50 50)"
                    className="donut-chart__segment"
                  />
                  {/* Running segment */}
                  <circle
                    cx="50"
                    cy="50"
                    r="40"
                    fill="none"
                    stroke="var(--warning)"
                    strokeWidth="12"
                    strokeDasharray={`${((data?.status?.running || 0) / (totalStatusCount || 1)) * 251} 251`}
                    strokeDashoffset={`-${(successRate + ((data?.status?.error || 0) / (totalStatusCount || 1))) * 251}`}
                    transform="rotate(-90 50 50)"
                    className="donut-chart__segment"
                  />
                </svg>
                <div className="donut-chart__center">
                  <span className="donut-chart__value">{successRate}%</span>
                  <span className="donut-chart__label">成功率</span>
                </div>
              </div>
              <div className="chart-legend">
                <div className="legend-item">
                  <span className="legend-dot legend-dot--success" />
                  <span className="legend-label">成功</span>
                  <span className="legend-value">{(data?.status?.success || 0) + (data?.status?.completed || 0)}</span>
                </div>
                <div className="legend-item">
                  <span className="legend-dot legend-dot--warning" />
                  <span className="legend-label">运行中</span>
                  <span className="legend-value">{data?.status?.running || 0}</span>
                </div>
                <div className="legend-item">
                  <span className="legend-dot legend-dot--error" />
                  <span className="legend-label">异常</span>
                  <span className="legend-value">{data?.status?.error || 0}</span>
                </div>
                <div className="legend-item">
                  <span className="legend-dot legend-dot--muted" />
                  <span className="legend-label">停止</span>
                  <span className="legend-value">{data?.status?.stopped || 0}</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Device Status Chart */}
        <div className="chart-card animate-scaleIn">
          <div className="chart-card__header">
            <div>
              <h3 className="chart-card__title">设备状态</h3>
              <p className="chart-card__subtitle">在线/离线监控</p>
            </div>
            <div className="chart-card__action">
              <button className="btn btn--ghost btn--sm">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                  <polyline points="7 10 12 15 17 10" />
                  <line x1="12" y1="15" x2="12" y2="3" />
                </svg>
                导出
              </button>
            </div>
          </div>
          <div className="chart-card__body">
            <div className="device-status-chart">
              <div className="status-bar">
                <div className="status-bar__segments">
                  <div
                    className="status-bar__segment status-bar__segment--success"
                    style={{ width: `${(deviceStats.online / (deviceStats.total || 1)) * 100}%` }}
                  />
                  <div
                    className="status-bar__segment status-bar__segment--error"
                    style={{ width: `${(deviceStats.error / (deviceStats.total || 1)) * 100}%` }}
                  />
                  <div
                    className="status-bar__segment status-bar__segment--muted"
                    style={{ width: `${(deviceStats.offline / (deviceStats.total || 1)) * 100}%` }}
                  />
                </div>
              </div>
              <div className="status-numbers">
                <div className="status-number">
                  <span className="status-number__value text-success">{deviceStats.online}</span>
                  <span className="status-number__label">在线</span>
                </div>
                <div className="status-number">
                  <span className="status-number__value text-error">{deviceStats.error}</span>
                  <span className="status-number__label">异常</span>
                </div>
                <div className="status-number">
                  <span className="status-number__value text-muted">{deviceStats.offline}</span>
                  <span className="status-number__label">离线</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Status Cards */}
      <div className="status-cards-row">
        <div className="status-card-compact">
          <div className="status-card-compact__icon status-card-compact__icon--success">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
              <polyline points="22 4 12 14.01 9 11.01" />
            </svg>
          </div>
          <div className="status-card-compact__content">
            <span className="status-card-compact__value">{data?.active_tasks || 0}</span>
            <span className="status-card-compact__label">活跃任务</span>
          </div>
          <div className="status-card-compact__indicator status-card-compact__indicator--success" />
        </div>

        <div className="status-card-compact">
          <div className="status-card-compact__icon status-card-compact__icon--warning">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="10" />
              <polyline points="12 6 12 12 16 14" />
            </svg>
          </div>
          <div className="status-card-compact__content">
            <span className="status-card-compact__value">{data?.recent_runs?.length || 0}</span>
            <span className="status-card-compact__label">今日运行</span>
          </div>
          <div className="status-card-compact__indicator status-card-compact__indicator--warning" />
        </div>

        <div className="status-card-compact">
          <div className="status-card-compact__icon status-card-compact__icon--info">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
            </svg>
          </div>
          <div className="status-card-compact__content">
            <span className="status-card-compact__value">{successRate}%</span>
            <span className="status-card-compact__label">成功率</span>
          </div>
          <div className="status-card-compact__indicator status-card-compact__indicator--success" />
        </div>

        <div className="status-card-compact">
          <div className="status-card-compact__icon status-card-compact__icon--error">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="8" x2="12" y2="12" />
              <line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
          </div>
          <div className="status-card-compact__content">
            <span className="status-card-compact__value">{data?.status?.error || 0}</span>
            <span className="status-card-compact__label">异常告警</span>
          </div>
          <div className="status-card-compact__indicator status-card-compact__indicator--error" />
        </div>
      </div>

      {/* Main Content Grid */}
      <div className="dashboard__grid">
        {/* Task List */}
        <div className="table-card animate-slideIn">
          <div className="table-card__header">
            <h3 className="table-card__title">任务列表</h3>
            <Link to="/acquisition" className="btn btn--primary btn--sm">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <polygon points="5 3 19 12 5 21 5 3" />
              </svg>
              采集控制
            </Link>
          </div>
          {tasks.length === 0 ? (
            <div className="empty-state">
              <svg className="empty-state__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                <rect x="3" y="3" width="18" height="18" rx="2" />
                <path d="M9 9h6M9 15h6M9 12h6" />
              </svg>
              <div className="empty-state__title">暂无任务</div>
              <div className="empty-state__description">导入配置文件以创建采集任务</div>
            </div>
          ) : (
            <div className="table-container">
              <table className="table">
                <thead>
                  <tr>
                    <th>任务编码</th>
                    <th>名称</th>
                    <th>状态</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {tasks.slice(0, 5).map((task) => (
                    <tr key={task.id}>
                      <td>
                        <div className="task-code">
                          <span className="task-code__icon">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                              <rect x="3" y="3" width="18" height="18" rx="2" />
                              <path d="M9 9h6M9 15h6M9 12h6" />
                            </svg>
                          </span>
                          <span className="task-code__text font-medium">{task.code}</span>
                        </div>
                      </td>
                      <td>{task.name}</td>
                      <td>
                        <span className={task.is_active ? 'status-badge status-badge--active' : 'status-badge status-badge--inactive'}>
                          <span className="status-badge__dot" />
                          {task.is_active ? '启用' : '停用'}
                        </span>
                      </td>
                      <td>
                        <Link to="/acquisition" className="btn btn--ghost btn--icon btn--sm">
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <polygon points="5 3 19 12 5 21 5 3" />
                          </svg>
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Recent Runs */}
        <div className="table-card animate-slideIn">
          <div className="table-card__header">
            <h3 className="table-card__title">最近运行</h3>
            <button className="btn btn--ghost btn--sm">
              查看全部
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <polyline points="9 18 15 12 9 6" />
              </svg>
            </button>
          </div>
          {data?.recent_runs && data.recent_runs.length === 0 ? (
            <div className="empty-state">
              <svg className="empty-state__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                <circle cx="12" cy="12" r="10" />
                <polyline points="12 6 12 12 16 14" />
              </svg>
              <div className="empty-state__title">暂无运行记录</div>
              <div className="empty-state__description">启动采集任务后将在此显示</div>
            </div>
          ) : (
            <div className="table-container">
              <table className="table">
                <thead>
                  <tr>
                    <th>任务</th>
                    <th>状态</th>
                    <th>时间</th>
                  </tr>
                </thead>
                <tbody>
                  {data?.recent_runs?.slice(0, 5).map((run) => (
                    <tr
                      key={
                        run.log_reference ||
                        `${run.task}|${run.started_at || ''}|${run.finished_at || ''}`
                      }
                    >
                      <td>
                        <div className="run-task">
                          <span className="run-task__name font-medium">{run.task}</span>
                          <span className="run-task__worker text-xs text-muted">{run.worker || '本地'}</span>
                        </div>
                      </td>
                      <td>
                        <span className={getStatusBadgeClass(run.status)}>
                          <span className="status-badge__dot" />
                          {run.status}
                        </span>
                      </td>
                      <td className="text-secondary">{formatTime(run.started_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {/* Quick Actions */}
      <div className="quick-actions animate-slideIn">
        <h3 className="quick-actions__title">快速操作</h3>
        <div className="quick-actions__grid">
          <Link to="/acquisition" className="quick-action-card">
            <div className="quick-action-card__icon quick-action-card__icon--primary">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <polygon points="5 3 19 12 5 21 5 3" />
              </svg>
            </div>
            <div className="quick-action-card__content">
              <span className="quick-action-card__title">采集控制</span>
              <span className="quick-action-card__description">启动/停止采集任务</span>
            </div>
            <svg className="quick-action-card__arrow" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <polyline points="9 18 15 12 9 6" />
            </svg>
          </Link>

          <Link to="/import" className="quick-action-card">
            <div className="quick-action-card__icon quick-action-card__icon--success">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                <polyline points="17 8 12 3 7 8" />
                <line x1="12" y1="3" x2="12" y2="15" />
              </svg>
            </div>
            <div className="quick-action-card__content">
              <span className="quick-action-card__title">导入配置</span>
              <span className="quick-action-card__description">上传Excel配置文件</span>
            </div>
            <svg className="quick-action-card__arrow" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <polyline points="9 18 15 12 9 6" />
            </svg>
          </Link>

          <Link to="/visualization" className="quick-action-card">
            <div className="quick-action-card__icon quick-action-card__icon--info">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <line x1="18" y1="20" x2="18" y2="10" />
                <line x1="12" y1="20" x2="12" y2="4" />
                <line x1="6" y1="20" x2="6" y2="14" />
              </svg>
            </div>
            <div className="quick-action-card__content">
              <span className="quick-action-card__title">数据可视化</span>
              <span className="quick-action-card__description">查看历史趋势数据</span>
            </div>
            <svg className="quick-action-card__arrow" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <polyline points="9 18 15 12 9 6" />
            </svg>
          </Link>

          <Link to="/versions" className="quick-action-card">
            <div className="quick-action-card__icon quick-action-card__icon--warning">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="10" />
                <polyline points="12 6 12 12 16 14" />
              </svg>
            </div>
            <div className="quick-action-card__content">
              <span className="quick-action-card__title">版本历史</span>
              <span className="quick-action-card__description">查看配置版本记录</span>
            </div>
            <svg className="quick-action-card__arrow" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <polyline points="9 18 15 12 9 6" />
            </svg>
          </Link>
        </div>
      </div>
    </div>
  );
};

export default DashboardPage;
