import { useEffect, useState, useCallback } from 'react';
import type { AcqTask, AcquisitionSession } from '../services/acquisitionApi';
import { fetchTasks, fetchActiveSessions } from '../services/acquisitionApi';
import { isAbortError } from '../services/http';
import TaskControlPanel from '../components/acquisition/TaskControlPanel';
import { useWebSocket, WebSocketStatus, WebSocketMessage } from '../hooks/useWebSocket';
import './AcquisitionControlPage.css';

const AcquisitionControlPage = () => {
  const [tasks, setTasks] = useState<AcqTask[]>([]);
  const [activeSessions, setActiveSessions] = useState<AcquisitionSession[]>([]);
  // Full-page spinner only on the initial mount. Background reloads (triggered
  // by onStatusChange after start/stop, or the manual refresh button) refresh
  // data in place so the TaskControlPanels and their WebSockets aren't torn down.
  const [initialLoading, setInitialLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [useWebSocketUpdates, setUseWebSocketUpdates] = useState(true);

  const loadData = useCallback(async (signal?: AbortSignal) => {
    setRefreshing(true);
    setError(null);

    try {
      const [tasksData, sessionsData] = await Promise.all([
        fetchTasks(signal),
        fetchActiveSessions(signal),
      ]);

      setTasks(tasksData);
      setActiveSessions(sessionsData);
    } catch (err) {
      // Ignore cancellations from a unmount/re-run cleanup.
      if (isAbortError(err)) return;
      setError((err as Error).message);
    } finally {
      if (!signal?.aborted) {
        setInitialLoading(false);
        setRefreshing(false);
      }
    }
  }, []);

  useEffect(() => {
    const aborter = new AbortController();
    loadData(aborter.signal);
    return () => aborter.abort();
  }, [loadData]);

  const handleWebSocketMessage = useCallback((message: WebSocketMessage) => {
    if (message.type === 'session_status') {
      const sessionData = message.data as AcquisitionSession;
      setActiveSessions((prev) => {
        const existingIndex = prev.findIndex((s) => s.id === sessionData.id);
        if (existingIndex >= 0) {
          const updated = [...prev];
          updated[existingIndex] = sessionData;
          return updated;
        }
        return [...prev, sessionData];
      });
    }
  }, []);

  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${wsProtocol}//${window.location.host}/ws/acquisition/global/`;

  // WebSocket is fully gated by the realtime toggle: when off, the socket is
  // closed and the polling effect below becomes the single update source.
  const { status: wsStatus } = useWebSocket({
    url: wsUrl,
    onMessage: handleWebSocketMessage,
    enabled: useWebSocketUpdates,
  });

  // 每 3s 轮询一次活跃会话。
  //
  // 以前这里在 WS 连接时会 return 掉、完全不轮询 —— 因为 WS 的 session_status
  // 已经能把状态变更即时推过来。但 WS 只在**状态变化**时推,而「实际入库频率」
  // (session.metadata.ingest_points_per_sec)是后端每 10s 刷一次的持续量,
  // 状态不变时 WS 根本不会带它过来。所以这个轮询要一直跑,才能让实时速率读数
  // 保持新鲜;WS 仍然负责状态变更的即时反馈,两者都写 activeSessions,幂等无害。
  useEffect(() => {
    let aborter: AbortController | null = null;
    const poll = () => {
      aborter?.abort();
      aborter = new AbortController();
      fetchActiveSessions(aborter.signal)
        .then(setActiveSessions)
        .catch((err) => {
          if (!isAbortError(err)) console.error(err);
        });
    };

    const interval = setInterval(poll, 3000);
    return () => {
      clearInterval(interval);
      aborter?.abort();
    };
  }, []);

  const getSessionForTask = (taskId: number) => {
    return activeSessions.find((s) => s.task === taskId);
  };

  const activeTasks = tasks.filter((t) => t.is_active);
  const inactiveTasks = tasks.filter((t) => !t.is_active);

  const runningCount = activeSessions.filter((s) => s.status === 'running').length;
  const errorCount = activeSessions.filter((s) => s.status === 'error').length;

  const getWsStatusText = () => {
    switch (wsStatus) {
      case WebSocketStatus.CONNECTED:
        return <span className="ws-status ws-status--connected">实时连接已建立</span>;
      case WebSocketStatus.CONNECTING:
        return <span className="ws-status ws-status--connecting">连接中...</span>;
      case WebSocketStatus.DISCONNECTED:
        return <span className="ws-status ws-status--disconnected">实时连接已断开</span>;
      case WebSocketStatus.ERROR:
        return <span className="ws-status ws-status--error">连接错误</span>;
      default:
        return null;
    }
  };

  if (initialLoading) {
    return (
      <div className="loading">
        <div className="loading__spinner" />
      </div>
    );
  }

  return (
    <div className="acquisition-control-page">
      {/* Header */}
      <div className="page-header">
        <div className="page-header__left">
          <h2>采集控制台</h2>
          {getWsStatusText()}
        </div>
        <div className="page-header__actions">
          <label className="ws-toggle">
            <input
              type="checkbox"
              checked={useWebSocketUpdates}
              onChange={(e) => setUseWebSocketUpdates(e.target.checked)}
            />
            <span className="ws-toggle__label">实时更新</span>
          </label>
          <button onClick={() => loadData()} disabled={refreshing} className="btn btn--secondary">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M23 4v6h-6M1 20v-6h6" />
              <path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15" />
            </svg>
            刷新
          </button>
        </div>
      </div>

      {error && (
        <div className="error-banner">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="8" x2="12" y2="12" />
            <line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
          <span>加载失败: {error}</span>
        </div>
      )}

      {/* Stats */}
      <div className="stats-bar">
        <div className="stat-card">
          <div className="stat-card__icon stat-card__icon--total">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M12 2v20M2 12h20" />
            </svg>
          </div>
          <div className="stat-card__content">
            <div className="stat-card__value">{tasks.length}</div>
            <div className="stat-card__label">任务总数</div>
          </div>
        </div>

        <div className="stat-card">
          <div className="stat-card__icon stat-card__icon--active">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="10" />
              <path d="M12 6v6l4 2" />
            </svg>
          </div>
          <div className="stat-card__content">
            <div className="stat-card__value">{activeTasks.length}</div>
            <div className="stat-card__label">启用任务</div>
          </div>
        </div>

        <div className="stat-card">
          <div className="stat-card__icon stat-card__icon--running">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <polygon points="5 3 19 12 5 21 5 3" />
            </svg>
          </div>
          <div className="stat-card__content">
            <div className="stat-card__value">{runningCount}</div>
            <div className="stat-card__label">运行中</div>
          </div>
        </div>

        <div className="stat-card">
          <div className="stat-card__icon stat-card__icon--error">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="10" />
              <line x1="15" y1="9" x2="9" y2="15" />
              <line x1="9" y1="9" x2="15" y2="15" />
            </svg>
          </div>
          <div className="stat-card__content">
            <div className="stat-card__value">{errorCount}</div>
            <div className="stat-card__label">错误</div>
          </div>
        </div>
      </div>

      {/* Active Tasks */}
      <div className="tasks-section">
        <h3 className="tasks-section__title">启用的任务 ({activeTasks.length})</h3>
        {activeTasks.length === 0 ? (
          <div className="empty-state">
            <svg className="empty-state__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
              <circle cx="12" cy="12" r="10" />
              <path d="M12 6v6l4 2" />
            </svg>
            <div className="empty-state__title">暂无激活的采集任务</div>
            <div className="empty-state__description">导入配置或启用任务以开始数据采集</div>
          </div>
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

      {/* Inactive Tasks */}
      {inactiveTasks.length > 0 && (
        <details className="tasks-section tasks-section--inactive">
          <summary className="tasks-section__header">
            <h3 className="tasks-section__title">未激活的任务 ({inactiveTasks.length})</h3>
            <svg className="tasks-section__arrow" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <polyline points="6 9 12 15 18 9" />
            </svg>
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
    </div>
  );
};

export default AcquisitionControlPage;
