import { useEffect, useState } from 'react';
import { useParams, useNavigate, Link } from 'react-router-dom';
import {
  fetchDevice,
  fetchDevicePoints,
  fetchDeviceStats,
  testDeviceConnection,
  deleteDevice,
  Device,
  Point,
  DeviceStats,
} from '../services/deviceApi';
import DeviceFormModal from '../components/DeviceFormModal';
import './DeviceDetailPage.css';

const DeviceDetailPage = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const deviceId = parseInt(id || '0', 10);

  const [device, setDevice] = useState<Device | null>(null);
  const [points, setPoints] = useState<Point[]>([]);
  const [stats, setStats] = useState<DeviceStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [testingConnection, setTestingConnection] = useState(false);
  const [connectionResult, setConnectionResult] = useState<{ success: boolean; message: string } | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  // 「修改配置」走的是和设备管理页同一个弹窗:按协议分发到各自的配置界面。
  const [editOpen, setEditOpen] = useState(false);

  useEffect(() => {
    loadDeviceData();
    // 仅在 deviceId 变化时重新加载；loadDeviceData 每次渲染重建，不入依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceId]);

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

  const handleTestConnection = async () => {
    setTestingConnection(true);
    setConnectionResult(null);

    try {
      const result = await testDeviceConnection(deviceId);
      setConnectionResult({ success: result.success, message: result.message });
    } catch (err) {
      setConnectionResult({ success: false, message: (err as Error).message });
    } finally {
      setTestingConnection(false);
    }
  };

  const handleDelete = async () => {
    if (!window.confirm(`确定要删除设备 "${device?.name}" 吗？\n\n注意：删除设备将同时删除其所有测点！`)) {
      return;
    }

    try {
      await deleteDevice(deviceId);
      navigate('/devices');
    } catch (err) {
      alert(`删除失败: ${(err as Error).message}`);
    }
  };

  const handleExportCSV = () => {
    if (points.length === 0) {
      alert('没有测点数据可导出');
      return;
    }

    const headers = ['编码', '地址', '描述', '采样率(Hz)', '发送到Kafka'];
    const rows = points.map(p => [
      p.code,
      p.address,
      p.description,
      p.sample_rate_hz,
      p.to_kafka ? '是' : '否',
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

  const getProtocolBadgeClass = (protocol: string) => {
    const p = protocol.toLowerCase();
    if (p.includes('modbus')) return 'protocol-badge protocol-badge--modbus';
    if (p.includes('mqtt')) return 'protocol-badge protocol-badge--mqtt';
    if (p.includes('plc')) return 'protocol-badge protocol-badge--plc';
    return 'protocol-badge';
  };

  if (loading) {
    return (
      <div className="loading">
        <div className="loading__spinner" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="error-state">
        <div className="error-state__title">加载失败</div>
        <div className="error-state__message">{error}</div>
      </div>
    );
  }

  if (!device) {
    return (
      <div className="empty-state">
        <div className="empty-state__title">设备不存在</div>
      </div>
    );
  }

  return (
    <div className="device-detail-page">
      {/* Header */}
      <div className="page-header">
        <div className="page-header__left">
          <Link to="/devices" className="back-link">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <polyline points="15 18 9 12 15 6" />
            </svg>
            返回设备列表
          </Link>
          <h2 className="page-header__title">{device.name}</h2>
          <span className={getProtocolBadgeClass(device.protocol)}>{device.protocol}</span>
        </div>
        <div className="page-header__actions">
          <button onClick={() => setEditOpen(true)} className="btn btn--secondary">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7" />
              <path d="M18.5 2.5a2.12 2.12 0 013 3L12 15l-4 1 1-4 9.5-9.5z" />
            </svg>
            修改配置
          </button>
          <button
            onClick={handleTestConnection}
            disabled={testingConnection}
            className="btn btn--secondary"
          >
            {testingConnection ? (
              <>
                <div className="loading__spinner" style={{ width: 16, height: 16 }} />
                测试中...
              </>
            ) : (
              <>
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
                  <polyline points="22 4 12 14.01 9 11.01" />
                </svg>
                测试连接
              </>
            )}
          </button>
          <button onClick={handleDelete} className="btn btn--danger">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <polyline points="3 6 5 6 21 6" />
              <path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2" />
            </svg>
            删除设备
          </button>
        </div>
      </div>

      {/* Connection Result */}
      {connectionResult && (
        <div className={`connection-result ${connectionResult.success ? 'connection-result--success' : 'connection-result--error'}`}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            {connectionResult.success ? (
              <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14M22 4L12 14.01l-3-3" />
            ) : (
              <>
                <circle cx="12" cy="12" r="10" />
                <line x1="15" y1="9" x2="9" y2="15" />
                <line x1="9" y1="9" x2="15" y2="15" />
              </>
            )}
          </svg>
          <span>{connectionResult.message}</span>
        </div>
      )}

      {/* Info Grid */}
      <div className="info-grid">
        <div className="card">
          <h3 className="card__title">基本信息</h3>
          <div className="info-list">
            <div className="info-item">
              <span className="info-item__label">设备编码</span>
              <span className="info-item__value">{device.code}</span>
            </div>
            <div className="info-item">
              <span className="info-item__label">IP 地址</span>
              <span className="info-item__value">{device.ip_address || 'N/A'}</span>
            </div>
            <div className="info-item">
              <span className="info-item__label">端口</span>
              <span className="info-item__value">{device.port || 'N/A'}</span>
            </div>
            <div className="info-item">
              <span className="info-item__label">站点 ID</span>
              <span className="info-item__value">{device.site}</span>
            </div>
          </div>
        </div>

        {stats && (
          <div className="card">
            <h3 className="card__title">统计信息</h3>
            <div className="stats-grid">
              <div className="stat-item">
                <div className="stat-item__value">{stats.total_points}</div>
                <div className="stat-item__label">测点总数</div>
              </div>
              <div className="stat-item">
                <div className="stat-item__value">{stats.task_count}</div>
                <div className="stat-item__label">关联任务</div>
              </div>
              <div className="stat-item stat-item--wide">
                <div className="stat-item__value stat-item__value--small">
                  {stats.last_acquisition ? new Date(stats.last_acquisition).toLocaleString('zh-CN') : '从未采集'}
                </div>
                <div className="stat-item__label">最近采集</div>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Related Tasks */}
      {stats && stats.related_tasks.length > 0 && (
        <div className="page-section">
          <h2 className="page-section__title">关联任务 ({stats.task_count})</h2>
          <div className="table-container">
            <table className="table">
              <thead>
                <tr>
                  <th>任务编码</th>
                  <th>任务名称</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {stats.related_tasks.slice(0, 5).map(task => (
                  <tr key={task.id}>
                    <td className="font-medium">{task.code}</td>
                    <td>{task.name}</td>
                    <td>
                      <span className={task.is_active ? 'status-badge status-badge--active' : 'status-badge status-badge--inactive'}>
                        <span className="status-badge__dot" />
                        {task.is_active ? '启用' : '停用'}
                      </span>
                    </td>
                    <td>
                      <Link to="/acquisition" className="btn btn--ghost btn--icon">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                          <polygon points="5 3 19 12 5 21 5 3" />
                        </svg>
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Points List */}
      <div className="page-section">
        <div className="page-header">
          <h2 className="page-section__title">测点列表 ({filteredPoints.length} / {points.length})</h2>
          <div className="flex gap-sm">
            <input
              type="text"
              className="input"
              placeholder="搜索测点..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              style={{ width: 200 }}
            />
            <button onClick={handleExportCSV} className="btn btn--secondary">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" />
                <polyline points="7 10 12 15 17 10" />
                <line x1="12" y1="15" x2="12" y2="3" />
              </svg>
              导出 CSV
            </button>
          </div>
        </div>

        {filteredPoints.length === 0 ? (
          <div className="empty-state">
            <svg className="empty-state__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
            </svg>
            <div className="empty-state__title">{searchTerm ? '没有匹配的测点' : '暂无测点数据'}</div>
          </div>
        ) : (
          <div className="table-container">
            <table className="table">
              <thead>
                <tr>
                  <th>编码</th>
                  <th>地址</th>
                  <th>描述</th>
                  <th>采样率 (Hz)</th>
                  <th>Kafka</th>
                </tr>
              </thead>
              <tbody>
                {filteredPoints.map(point => (
                  <tr key={point.id}>
                    <td className="font-medium">{point.code}</td>
                    <td className="text-secondary">{point.address}</td>
                    <td className="text-secondary">{point.description}</td>
                    <td>{point.sample_rate_hz}</td>
                    <td>
                      {point.to_kafka ? (
                        <span className="status-badge status-badge--active">
                          <span className="status-badge__dot" />
                          是
                        </span>
                      ) : (
                        <span className="status-badge status-badge--inactive">
                          <span className="status-badge__dot" />
                          否
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

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
