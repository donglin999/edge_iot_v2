import { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
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

const DeviceDetailPage = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const deviceId = parseInt(id || '0', 10);

  const [device, setDevice] = useState<Device | null>(null);
  const [points, setPoints] = useState<Point[]>([]);
  const [stats, setStats] = useState<DeviceStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testingConnection, setTestingConnection] = useState(false);
  const [connectionResult, setConnectionResult] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');

  useEffect(() => {
    loadDeviceData();
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
      setConnectionResult(result.success ? `✓ ${result.message}` : `✗ ${result.message}`);
    } catch (err) {
      setConnectionResult(`✗ ${(err as Error).message}`);
    } finally {
      setTestingConnection(false);
    }
  };

  const handleDelete = async () => {
    if (!confirm(`确定要删除设备 "${device?.name}" 吗？\n\n注意：删除设备将同时删除其所有测点！`)) {
      return;
    }

    try {
      await deleteDevice(deviceId);
      alert('设备已删除');
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

    // 创建 CSV 内容
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

    // 下载文件
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `device_${device?.code}_points.csv`;
    link.click();
  };

  // 过滤测点
  const filteredPoints = points.filter(point =>
    point.code.toLowerCase().includes(searchTerm.toLowerCase()) ||
    point.description.toLowerCase().includes(searchTerm.toLowerCase()) ||
    point.address.toLowerCase().includes(searchTerm.toLowerCase())
  );

  if (loading) return <p>加载中...</p>;
  if (error) return <p className="error">错误: {error}</p>;
  if (!device) return <p>设备不存在</p>;

  return (
    <section>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
        <h2>设备详情: {device.code}</h2>
        <button onClick={() => navigate('/devices')} className="btn btn-secondary">
          返回设备列表
        </button>
      </div>

      {/* 基本信息卡片 */}
      <div className="card" style={{ marginBottom: '1.5rem' }}>
        <h3>基本信息</h3>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem', marginBottom: '1rem' }}>
          <div>
            <strong>设备名称:</strong> {device.name}
          </div>
          <div>
            <strong>设备编码:</strong> {device.code}
          </div>
          <div>
            <strong>协议类型:</strong> {device.protocol.toUpperCase()}
          </div>
          <div>
            <strong>IP 地址:</strong> {device.ip_address}
          </div>
          <div>
            <strong>端口:</strong> {device.port || 'N/A'}
          </div>
          <div>
            <strong>站点 ID:</strong> {device.site}
          </div>
          <div>
            <strong>创建时间:</strong> {new Date(device.created_at).toLocaleString('zh-CN')}
          </div>
          <div>
            <strong>更新时间:</strong> {new Date(device.updated_at).toLocaleString('zh-CN')}
          </div>
        </div>

        <div style={{ display: 'flex', gap: '0.5rem', marginTop: '1rem' }}>
          <button
            onClick={handleTestConnection}
            disabled={testingConnection}
            className="btn btn-primary"
          >
            {testingConnection ? '测试中...' : '测试连接'}
          </button>
          <button
            onClick={handleDelete}
            className="btn"
            style={{ backgroundColor: '#d32f2f', color: 'white' }}
          >
            删除设备
          </button>
        </div>

        {connectionResult && (
          <div
            style={{
              marginTop: '1rem',
              padding: '0.5rem',
              backgroundColor: connectionResult.startsWith('✓') ? '#e8f5e9' : '#ffebee',
              border: `1px solid ${connectionResult.startsWith('✓') ? '#4caf50' : '#f44336'}`,
              borderRadius: '4px',
            }}
          >
            {connectionResult}
          </div>
        )}
      </div>

      {/* 统计信息卡片 */}
      {stats && (
        <div className="card" style={{ marginBottom: '1.5rem' }}>
          <h3>统计信息</h3>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '1rem' }}>
            <div
              style={{
                padding: '1rem',
                backgroundColor: '#e3f2fd',
                borderRadius: '4px',
                textAlign: 'center',
              }}
            >
              <div style={{ fontSize: '2rem', fontWeight: 'bold', color: '#1976d2' }}>
                {stats.total_points}
              </div>
              <div style={{ color: '#666' }}>测点总数</div>
            </div>
            <div
              style={{
                padding: '1rem',
                backgroundColor: '#f3e5f5',
                borderRadius: '4px',
                textAlign: 'center',
              }}
            >
              <div style={{ fontSize: '2rem', fontWeight: 'bold', color: '#7b1fa2' }}>
                {stats.task_count}
              </div>
              <div style={{ color: '#666' }}>关联任务</div>
            </div>
            <div
              style={{
                padding: '1rem',
                backgroundColor: '#e8f5e9',
                borderRadius: '4px',
                textAlign: 'center',
              }}
            >
              <div style={{ fontSize: '1rem', fontWeight: 'bold', color: '#388e3c' }}>
                {stats.last_acquisition
                  ? new Date(stats.last_acquisition).toLocaleString('zh-CN')
                  : '从未采集'}
              </div>
              <div style={{ color: '#666' }}>最近采集时间</div>
            </div>
          </div>
        </div>
      )}

      {/* 关联任务列表 */}
      {stats && stats.related_tasks.length > 0 && (
        <div className="card" style={{ marginBottom: '1.5rem' }}>
          <h3>关联任务 ({stats.task_count})</h3>
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
              {stats.related_tasks.map(task => (
                <tr key={task.id}>
                  <td>{task.code}</td>
                  <td>{task.name}</td>
                  <td>{task.is_active ? '启用' : '停用'}</td>
                  <td>
                    <a href={`/acquisition#task-${task.id}`} style={{ color: '#007bff' }}>
                      查看
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {stats.task_count > 5 && (
            <p style={{ marginTop: '0.5rem', color: '#666', fontSize: '0.9rem' }}>
              显示前 5 个任务，共 {stats.task_count} 个
            </p>
          )}
        </div>
      )}

      {/* 测点列表 */}
      <div className="card">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
          <h3>测点列表 ({filteredPoints.length} / {points.length})</h3>
          <div style={{ display: 'flex', gap: '0.5rem' }}>
            <input
              type="text"
              placeholder="搜索测点..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              style={{ padding: '0.5rem', border: '1px solid #ddd', borderRadius: '4px' }}
            />
            <button onClick={handleExportCSV} className="btn btn-secondary">
              导出 CSV
            </button>
          </div>
        </div>

        {filteredPoints.length === 0 ? (
          <p style={{ color: '#666', textAlign: 'center', padding: '2rem' }}>
            {searchTerm ? '没有匹配的测点' : '暂无测点数据'}
          </p>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="table">
              <thead>
                <tr>
                  <th>编码</th>
                  <th>地址</th>
                  <th>描述</th>
                  <th>采样率 (Hz)</th>
                  <th>发送到 Kafka</th>
                  <th>创建时间</th>
                </tr>
              </thead>
              <tbody>
                {filteredPoints.map(point => (
                  <tr key={point.id}>
                    <td><strong>{point.code}</strong></td>
                    <td>{point.address}</td>
                    <td>{point.description}</td>
                    <td>{point.sample_rate_hz}</td>
                    <td>{point.to_kafka ? '✓ 是' : '✗ 否'}</td>
                    <td>{new Date(point.created_at).toLocaleString('zh-CN')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
};

export default DeviceDetailPage;
