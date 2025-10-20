import { useEffect, useState, useCallback } from 'react';
import type { AcqTask, AcquisitionSession } from '../services/acquisitionApi';
import { fetchTasks, fetchActiveSessions } from '../services/acquisitionApi';
import TaskControlPanel from '../components/acquisition/TaskControlPanel';
import { useWebSocket, WebSocketStatus, WebSocketMessage } from '../hooks/useWebSocket';

const AcquisitionControlPage = () => {
  const [tasks, setTasks] = useState<AcqTask[]>([]);
  const [activeSessions, setActiveSessions] = useState<AcquisitionSession[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [useWebSocketUpdates, setUseWebSocketUpdates] = useState(true);

  const loadData = async () => {
    setLoading(true);
    setError(null);

    try {
      const [tasksData, sessionsData] = await Promise.all([
        fetchTasks(),
        fetchActiveSessions(),
      ]);

      setTasks(tasksData);
      setActiveSessions(sessionsData);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
  }, []);

  // WebSocket message handler
  const handleWebSocketMessage = useCallback((message: WebSocketMessage) => {
    if (message.type === 'session_status') {
      const sessionData = message.data as AcquisitionSession;

      // Update or add session in the list
      setActiveSessions((prev) => {
        const existingIndex = prev.findIndex((s) => s.id === sessionData.id);
        if (existingIndex >= 0) {
          // Update existing session
          const updated = [...prev];
          updated[existingIndex] = sessionData;
          return updated;
        } else {
          // Add new session
          return [...prev, sessionData];
        }
      });
    } else if (message.type === 'data_point') {
      // Handle data point updates if needed
      console.log('New data point:', message.data);
    }
  }, []);

  // WebSocket connection
  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${wsProtocol}//${window.location.host}/ws/acquisition/global/`;

  const { status: wsStatus } = useWebSocket({
    url: wsUrl,
    onMessage: handleWebSocketMessage,
    onOpen: () => console.log('WebSocket connected'),
    onClose: () => console.log('WebSocket disconnected'),
    onError: (error) => console.error('WebSocket error:', error),
    autoReconnect: useWebSocketUpdates,
  });

  // Fallback polling when WebSocket is disabled or not connected
  useEffect(() => {
    if (useWebSocketUpdates && wsStatus === WebSocketStatus.CONNECTED) {
      // WebSocket is active, no polling needed
      return;
    }

    // Fallback to polling every 3 seconds
    const interval = setInterval(() => {
      fetchActiveSessions()
        .then(setActiveSessions)
        .catch(console.error);
    }, 3000);

    return () => clearInterval(interval);
  }, [useWebSocketUpdates, wsStatus]);

  const getSessionForTask = (taskId: number) => {
    return activeSessions.find((s) => s.task === taskId);
  };

  const activeTasks = tasks.filter((t) => t.is_active);
  const inactiveTasks = tasks.filter((t) => !t.is_active);

  return (
    <section className="acquisition-control-page">
      <div className="page-header">
        <h2>采集任务控制台</h2>
        <div className="header-actions">
          <label className="auto-refresh-toggle">
            <input
              type="checkbox"
              checked={useWebSocketUpdates}
              onChange={(e) => setUseWebSocketUpdates(e.target.checked)}
            />
            <span>
              WebSocket 实时更新
              {wsStatus === WebSocketStatus.CONNECTED && ' ✓ 已连接'}
              {wsStatus === WebSocketStatus.CONNECTING && ' ⏳ 连接中'}
              {wsStatus === WebSocketStatus.DISCONNECTED && ' ✗ 未连接'}
              {wsStatus === WebSocketStatus.ERROR && ' ⚠ 错误'}
            </span>
          </label>
          <button onClick={loadData} disabled={loading} className="btn btn-secondary">
            {loading ? '刷新中...' : '手动刷新'}
          </button>
        </div>
      </div>

      {error && (
        <div className="error-banner">
          <strong>加载失败:</strong> {error}
        </div>
      )}

      <div className="stats-bar">
        <div className="stat-card">
          <div className="stat-label">任务总数</div>
          <div className="stat-value">{tasks.length}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">启用任务</div>
          <div className="stat-value">{activeTasks.length}</div>
        </div>
        <div className="stat-card stat-distribution">
          <div className="stat-label">状态分布</div>
          <div className="stat-distribution-content">
            <div className="stat-item">
              <span className="stat-mini-label">运行:</span>
              <span className="stat-mini-value stat-running">
                {activeSessions.filter((s) => s.status === 'running').length}
              </span>
            </div>
            <div className="stat-item">
              <span className="stat-mini-label">停止:</span>
              <span className="stat-mini-value stat-stopped">
                {tasks.length - activeSessions.filter((s) => s.status === 'running').length}
              </span>
            </div>
            <div className="stat-item">
              <span className="stat-mini-label">错误:</span>
              <span className="stat-mini-value stat-error">
                {activeSessions.filter((s) => s.status === 'error').length}
              </span>
            </div>
          </div>
        </div>
      </div>

      <div className="tasks-section">
        <h3>激活的任务</h3>
        {activeTasks.length === 0 ? (
          <div className="empty-state">暂无激活的采集任务</div>
        ) : (
          <div className="tasks-list">
            {activeTasks.map((task) => (
              <TaskControlPanel
                key={task.id}
                task={task}
                activeSession={getSessionForTask(task.id)}
                onStatusChange={loadData}
              />
            ))}
          </div>
        )}
      </div>

      {inactiveTasks.length > 0 && (
        <details className="tasks-section inactive-section">
          <summary>
            <h3>未激活的任务 ({inactiveTasks.length})</h3>
          </summary>
          <div className="tasks-list">
            {inactiveTasks.map((task) => (
              <TaskControlPanel
                key={task.id}
                task={task}
                activeSession={getSessionForTask(task.id)}
                onStatusChange={loadData}
              />
            ))}
          </div>
        </details>
      )}

      <style>{`
        .acquisition-control-page {
          padding: 2rem;
          max-width: 1400px;
          margin: 0 auto;
        }

        .page-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 2rem;
        }

        .page-header h2 {
          margin: 0;
        }

        .header-actions {
          display: flex;
          gap: 1rem;
          align-items: center;
        }

        .auto-refresh-toggle {
          display: flex;
          align-items: center;
          gap: 0.5rem;
          cursor: pointer;
          user-select: none;
        }

        .auto-refresh-toggle input[type="checkbox"] {
          cursor: pointer;
        }

        .error-banner {
          background: #ffebee;
          color: #c62828;
          padding: 1rem;
          border-radius: 4px;
          margin-bottom: 1rem;
          border: 1px solid #ef5350;
        }

        .stats-bar {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
          gap: 1rem;
          margin-bottom: 2rem;
        }

        .stat-card {
          background: white;
          padding: 1.5rem;
          border-radius: 8px;
          border: 1px solid #e0e0e0;
          text-align: center;
        }

        .stat-label {
          font-size: 0.9rem;
          color: #666;
          margin-bottom: 0.5rem;
        }

        .stat-value {
          font-size: 2rem;
          font-weight: bold;
          color: #333;
        }

        .stat-value.stat-running {
          color: #4caf50;
        }

        .stat-value.stat-starting {
          color: #ff9800;
        }

        .stat-card.stat-distribution {
          text-align: left;
        }

        .stat-distribution-content {
          display: flex;
          flex-direction: column;
          gap: 0.5rem;
          margin-top: 0.5rem;
        }

        .stat-item {
          display: flex;
          justify-content: space-between;
          align-items: center;
        }

        .stat-mini-label {
          font-size: 0.9rem;
          color: #666;
        }

        .stat-mini-value {
          font-size: 1.2rem;
          font-weight: bold;
        }

        .stat-mini-value.stat-running {
          color: #4caf50;
        }

        .stat-mini-value.stat-starting {
          color: #ff9800;
        }

        .stat-mini-value.stat-stopped {
          color: #999;
        }

        .stat-mini-value.stat-error {
          color: #f44336;
        }

        .tasks-section {
          margin-bottom: 2rem;
        }

        .tasks-section h3 {
          margin-bottom: 1rem;
        }

        .tasks-section.inactive-section summary {
          cursor: pointer;
          list-style: none;
          display: flex;
          align-items: center;
          padding: 0.5rem 0;
        }

        .tasks-section.inactive-section summary::-webkit-details-marker {
          display: none;
        }

        .tasks-section.inactive-section summary h3 {
          margin: 0;
          color: #666;
        }

        .tasks-section.inactive-section summary::before {
          content: '▶';
          margin-right: 0.5rem;
          transition: transform 0.2s;
        }

        .tasks-section.inactive-section[open] summary::before {
          transform: rotate(90deg);
        }

        .tasks-list {
          display: flex;
          flex-direction: column;
          gap: 1rem;
        }

        .empty-state {
          padding: 3rem;
          text-align: center;
          color: #999;
          background: #fafafa;
          border-radius: 8px;
          border: 2px dashed #ddd;
        }

        .btn {
          padding: 0.5rem 1rem;
          border: none;
          border-radius: 4px;
          cursor: pointer;
          font-size: 0.9rem;
          transition: all 0.2s;
        }

        .btn:disabled {
          opacity: 0.5;
          cursor: not-allowed;
        }

        .btn-secondary {
          background: #757575;
          color: white;
        }

        .btn-secondary:hover:not(:disabled) {
          background: #616161;
        }
      `}</style>
    </section>
  );
};

export default AcquisitionControlPage;
