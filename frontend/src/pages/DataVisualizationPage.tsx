/**
 * Data Visualization Page
 * Displays real-time and historical data charts
 */
import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import RealtimeChart from '../components/RealtimeChart';
import HistoricalTrendChart from '../components/HistoricalTrendChart';
import {
  fetchActiveSessions,
  fetchSessions,
  AcquisitionSession,
  fetchPointHistory,
} from '../services/dataApi';
import { fetchDevices, Device } from '../services/deviceApi';

const DataVisualizationPage: React.FC = () => {
  const navigate = useNavigate();

  // State
  const [activeSessions, setActiveSessions] = useState<AcquisitionSession[]>([]);
  const [recentSessions, setRecentSessions] = useState<AcquisitionSession[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<number | null>(null);
  const [selectedPointCode, setSelectedPointCode] = useState<string>('');
  const [timeRange, setTimeRange] = useState<'1h' | '6h' | '24h' | 'custom'>('1h');
  const [customStartTime, setCustomStartTime] = useState('');
  const [customEndTime, setCustomEndTime] = useState('');
  const [chartType, setChartType] = useState<'line' | 'area'>('line');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [availablePointCodes, setAvailablePointCodes] = useState<string[]>([]);

  // Load data
  useEffect(() => {
    const loadData = async () => {
      setLoading(true);
      setError(null);

      try {
        const [activeSessionsData, recentSessionsData, devicesData] = await Promise.all([
          fetchActiveSessions(),
          fetchSessions(10),
          fetchDevices(),
        ]);

        setActiveSessions(activeSessionsData);
        setRecentSessions(recentSessionsData);
        setDevices(devicesData);

        // Auto-select first active session
        if (activeSessionsData.length > 0 && !selectedSessionId) {
          setSelectedSessionId(activeSessionsData[0].id);
        }
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setLoading(false);
      }
    };

    loadData();
  }, []);

  // Load available point codes when device or session changes
  useEffect(() => {
    const loadPointCodes = async () => {
      try {
        // Get point codes from devices
        const allDevices = await fetchDevices();
        const codes: string[] = [];

        for (const device of allDevices) {
          const response = await fetch(`/api/config/devices/${device.id}/points/`);
          if (response.ok) {
            const points = await response.json();
            points.forEach((point: any) => {
              if (point.code && !codes.includes(point.code)) {
                codes.push(point.code);
              }
            });
          }
        }

        setAvailablePointCodes(codes.sort());

        // Auto-select first point code if none selected
        if (codes.length > 0 && !selectedPointCode) {
          setSelectedPointCode(codes[0]);
        }
      } catch (err) {
        console.error('Failed to load point codes:', err);
      }
    };

    loadPointCodes();
  }, [devices]);

  // Calculate time range
  const getTimeRange = () => {
    const now = new Date();
    let startTime: string | undefined;

    if (timeRange === 'custom') {
      startTime = customStartTime || undefined;
      return {
        startTime,
        endTime: customEndTime || undefined,
      };
    }

    const hours = timeRange === '1h' ? 1 : timeRange === '6h' ? 6 : 24;
    const start = new Date(now.getTime() - hours * 60 * 60 * 1000);
    startTime = start.toISOString();

    return { startTime, endTime: now.toISOString() };
  };

  const { startTime, endTime } = getTimeRange();

  // Export data as CSV
  const handleExportData = async () => {
    if (!selectedPointCode) {
      alert('请先选择测点');
      return;
    }

    try {
      const data = await fetchPointHistory(selectedPointCode, startTime, endTime, 10000);

      const headers = ['时间', '数值', '质量'];
      const rows = data.data.map((dp) => [
        new Date(dp.timestamp).toLocaleString('zh-CN'),
        dp.value,
        dp.quality,
      ]);

      const csvContent = [
        headers.join(','),
        ...rows.map((row) => row.join(',')),
      ].join('\n');

      const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = `${selectedPointCode}_${new Date().toISOString().split('T')[0]}.csv`;
      link.click();
    } catch (err) {
      alert(`导出失败: ${(err as Error).message}`);
    }
  };

  if (loading) {
    return (
      <section>
        <h2>数据可视化</h2>
        <div className="card">
          <p>加载中...</p>
        </div>
      </section>
    );
  }

  if (error) {
    return (
      <section>
        <h2>数据可视化</h2>
        <div className="card">
          <p style={{ color: '#f44336' }}>错误: {error}</p>
        </div>
      </section>
    );
  }

  return (
    <section>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h2>数据可视化</h2>
        <button onClick={() => navigate('/')} style={{ padding: '0.5rem 1rem' }}>
          返回首页
        </button>
      </div>

      {/* Control Panel */}
      <div className="card">
        <h3>控制面板</h3>

        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
          {/* Active Session Selection */}
          <div>
            <label htmlFor="session-select" style={{ display: 'block', marginBottom: '0.5rem' }}>
              <strong>实时会话:</strong>
            </label>
            <select
              id="session-select"
              value={selectedSessionId || ''}
              onChange={(e) => setSelectedSessionId(Number(e.target.value))}
              style={{ width: '100%', padding: '0.5rem' }}
            >
              <option value="">选择会话...</option>
              {activeSessions.map((session) => (
                <option key={session.id} value={session.id}>
                  会话 #{session.id} - {session.task_code} ({session.status})
                </option>
              ))}
            </select>
            {activeSessions.length === 0 && (
              <p style={{ fontSize: '0.875rem', color: '#666', marginTop: '0.5rem' }}>
                暂无活跃会话
              </p>
            )}
          </div>

          {/* Point Code Selection */}
          <div>
            <label htmlFor="point-select" style={{ display: 'block', marginBottom: '0.5rem' }}>
              <strong>测点选择:</strong>
            </label>
            <select
              id="point-select"
              value={selectedPointCode}
              onChange={(e) => setSelectedPointCode(e.target.value)}
              style={{ width: '100%', padding: '0.5rem' }}
            >
              <option value="">选择测点...</option>
              {availablePointCodes.map((code) => (
                <option key={code} value={code}>
                  {code}
                </option>
              ))}
            </select>
          </div>

          {/* Time Range Selection */}
          <div>
            <label htmlFor="time-range" style={{ display: 'block', marginBottom: '0.5rem' }}>
              <strong>时间范围:</strong>
            </label>
            <select
              id="time-range"
              value={timeRange}
              onChange={(e) => setTimeRange(e.target.value as any)}
              style={{ width: '100%', padding: '0.5rem' }}
            >
              <option value="1h">最近1小时</option>
              <option value="6h">最近6小时</option>
              <option value="24h">最近24小时</option>
              <option value="custom">自定义</option>
            </select>
          </div>

          {/* Chart Type Selection */}
          <div>
            <label htmlFor="chart-type" style={{ display: 'block', marginBottom: '0.5rem' }}>
              <strong>图表类型:</strong>
            </label>
            <select
              id="chart-type"
              value={chartType}
              onChange={(e) => setChartType(e.target.value as any)}
              style={{ width: '100%', padding: '0.5rem' }}
            >
              <option value="line">折线图</option>
              <option value="area">面积图</option>
            </select>
          </div>
        </div>

        {/* Custom Time Range */}
        {timeRange === 'custom' && (
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem', marginTop: '1rem' }}>
            <div>
              <label htmlFor="start-time" style={{ display: 'block', marginBottom: '0.5rem' }}>
                开始时间:
              </label>
              <input
                id="start-time"
                type="datetime-local"
                value={customStartTime}
                onChange={(e) => setCustomStartTime(e.target.value)}
                style={{ width: '100%', padding: '0.5rem' }}
              />
            </div>
            <div>
              <label htmlFor="end-time" style={{ display: 'block', marginBottom: '0.5rem' }}>
                结束时间:
              </label>
              <input
                id="end-time"
                type="datetime-local"
                value={customEndTime}
                onChange={(e) => setCustomEndTime(e.target.value)}
                style={{ width: '100%', padding: '0.5rem' }}
              />
            </div>
          </div>
        )}

        {/* Export Button */}
        <div style={{ marginTop: '1rem' }}>
          <button
            onClick={handleExportData}
            disabled={!selectedPointCode}
            style={{
              padding: '0.5rem 1rem',
              backgroundColor: selectedPointCode ? '#4caf50' : '#ccc',
              color: 'white',
              border: 'none',
              borderRadius: '4px',
              cursor: selectedPointCode ? 'pointer' : 'not-allowed',
            }}
          >
            导出数据 CSV
          </button>
        </div>
      </div>

      {/* Real-time Chart */}
      {selectedSessionId && (
        <RealtimeChart
          sessionId={selectedSessionId}
          title={`实时数据监控 - 会话 #${selectedSessionId}`}
          maxDataPoints={50}
          height={350}
        />
      )}

      {/* Historical Trend Chart */}
      {selectedPointCode && (
        <HistoricalTrendChart
          pointCode={selectedPointCode}
          startTime={startTime}
          endTime={endTime}
          title={`历史趋势分析 - ${selectedPointCode}`}
          height={450}
          chartType={chartType}
          showBrush={true}
        />
      )}

      {/* Recent Sessions List */}
      <div className="card">
        <h3>最近会话</h3>
        <table className="table">
          <thead>
            <tr>
              <th>会话ID</th>
              <th>任务</th>
              <th>状态</th>
              <th>开始时间</th>
              <th>结束时间</th>
            </tr>
          </thead>
          <tbody>
            {recentSessions.length === 0 ? (
              <tr>
                <td colSpan={5} style={{ textAlign: 'center', color: '#999' }}>
                  暂无会话记录
                </td>
              </tr>
            ) : (
              recentSessions.map((session) => (
                <tr key={session.id}>
                  <td>#{session.id}</td>
                  <td>{session.task_code}</td>
                  <td>
                    <span
                      style={{
                        padding: '0.25rem 0.5rem',
                        borderRadius: '4px',
                        backgroundColor:
                          session.status === 'running'
                            ? '#4caf50'
                            : session.status === 'stopped'
                            ? '#9e9e9e'
                            : '#f44336',
                        color: 'white',
                        fontSize: '0.875rem',
                      }}
                    >
                      {session.status}
                    </span>
                  </td>
                  <td>
                    {session.started_at
                      ? new Date(session.started_at).toLocaleString('zh-CN')
                      : '-'}
                  </td>
                  <td>
                    {session.stopped_at
                      ? new Date(session.stopped_at).toLocaleString('zh-CN')
                      : '-'}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
};

export default DataVisualizationPage;
