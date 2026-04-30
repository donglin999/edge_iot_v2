import { useEffect, useRef, useState } from 'react';
import { Button, InputNumber, message } from 'antd';
import type { AcqTask, AcquisitionSession } from '../../services/acquisitionApi';
import {
  startTask,
  stopSession,
  updateTaskSampleRate,
} from '../../services/acquisitionApi';
import { useWebSocket, WebSocketMessage } from '../../hooks/useWebSocket';
import { buildWebSocketUrl } from '../../services/apiClient';
import './TaskControlPanel.css';

interface TaskControlPanelProps {
  task: AcqTask;
  activeSession?: AcquisitionSession;
  onStatusChange: () => void;
}

interface LogEntry {
  timestamp: string;
  level: 'info' | 'warning' | 'error' | 'success';
  message: string;
}

type ConnectionEventName =
  | 'connecting'
  | 'connected'
  | 'read_failed'
  | 'disconnected'
  | 'reconnecting'
  | 'reconnected'
  | 'gave_up';

interface ConnectionEventPayload {
  session_id?: number;
  timestamp?: string;
  event?: ConnectionEventName | string;
  device_code?: string;
  attempt?: number;
  max_attempts?: number;
  connect_duration_ms?: number;
  count?: number;
  last_error?: string;
  reason?: string;
  after_seconds?: number;
  downtime_seconds?: number;
  will_retry_in_seconds?: number;
}

interface SessionEventSubscriberProps {
  sessionId: number;
  onDataPoint: (pointCode: string) => void;
  onStatusChange: (status: string) => void;
  onConnectionEvent: (payload: ConnectionEventPayload) => void;
}

/**
 * Internal WS subscriber: mounts the useWebSocket hook only while the
 * parent has an active running session. Unmounting tears the socket down.
 */
const SessionEventSubscriber: React.FC<SessionEventSubscriberProps> = ({
  sessionId,
  onDataPoint,
  onStatusChange,
  onConnectionEvent,
}) => {
  const onDataPointRef = useRef(onDataPoint);
  const onStatusChangeRef = useRef(onStatusChange);
  const onConnectionEventRef = useRef(onConnectionEvent);
  useEffect(() => {
    onDataPointRef.current = onDataPoint;
    onStatusChangeRef.current = onStatusChange;
    onConnectionEventRef.current = onConnectionEvent;
  }, [onDataPoint, onStatusChange, onConnectionEvent]);

  const handleMessage = (message: WebSocketMessage) => {
    if (message.type === 'data_point' || message.type === 'data_point_update') {
      const payload = message.data as { point_code?: string } | null;
      onDataPointRef.current(payload?.point_code ?? '');
      return;
    }
    if (
      message.type === 'session_status' ||
      message.type === 'session_status_update'
    ) {
      const payload = message.data as { status?: string } | null;
      if (payload?.status) {
        onStatusChangeRef.current(payload.status);
      }
      return;
    }
    if (message.type === 'connection_event') {
      const payload = (message.data ?? {}) as ConnectionEventPayload;
      onConnectionEventRef.current(payload);
    }
  };

  useWebSocket({
    url: buildWebSocketUrl(`/ws/acquisition/sessions/${sessionId}/`),
    onMessage: handleMessage,
    autoReconnect: false,
  });

  return null;
};

const TaskControlPanel: React.FC<TaskControlPanelProps> = ({
  task,
  activeSession,
  onStatusChange,
}) => {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [showLogs, setShowLogs] = useState(false);

  // Sample rate (Hz) editor state
  const initialSampleRate =
    typeof task.sample_rate_hz === 'number' && !Number.isNaN(task.sample_rate_hz)
      ? task.sample_rate_hz
      : 1.0;
  const [sampleRateValue, setSampleRateValue] = useState<number | null>(
    initialSampleRate
  );
  const [savedSampleRate, setSavedSampleRate] = useState<number>(initialSampleRate);
  const [sampleRateSaving, setSampleRateSaving] = useState(false);

  // Sync local state when the upstream task value changes (e.g. list reload)
  useEffect(() => {
    const next =
      typeof task.sample_rate_hz === 'number' &&
      !Number.isNaN(task.sample_rate_hz)
        ? task.sample_rate_hz
        : 1.0;
    setSavedSampleRate(next);
    setSampleRateValue((prev) => (prev === null ? next : prev === savedSampleRate ? next : prev));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task.sample_rate_hz]);

  const isRunning = !!(activeSession && activeSession.status === 'running');

  const sampleRateDirty =
    sampleRateValue !== null &&
    !Number.isNaN(sampleRateValue) &&
    Math.abs((sampleRateValue ?? 0) - savedSampleRate) > 1e-6;

  // Buffer for high-frequency data_point events; flushed once per second.
  const dataPointBufferRef = useRef<{
    count: number;
    pointCodes: Set<string>;
  }>({ count: 0, pointCodes: new Set() });

  const addLog = (
    level: LogEntry['level'],
    message: string,
    timestamp?: string
  ) => {
    const entry: LogEntry = {
      timestamp: timestamp ?? new Date().toISOString(),
      level,
      message,
    };
    setLogs((prev) => [entry, ...prev].slice(0, 100)); // Keep last 100 logs
  };

  const clearLogs = () => setLogs([]);

  // Flush throttled data_point counter once per second while running.
  useEffect(() => {
    if (!isRunning) return;
    const interval = window.setInterval(() => {
      const buf = dataPointBufferRef.current;
      if (buf.count > 0) {
        const codes = Array.from(buf.pointCodes);
        const sample = codes.slice(0, 3).join(', ');
        const more = codes.length > 3 ? `等${codes.length}` : '';
        const detail = sample ? ` (${sample}${more})` : '';
        addLog('info', `📊 收到 ${buf.count} 个测点${detail}`);
        buf.count = 0;
        buf.pointCodes.clear();
      }
    }, 1000);
    return () => window.clearInterval(interval);
  }, [isRunning]);

  // Handlers for the WS subscriber
  const handleWsDataPoint = (pointCode: string) => {
    const buf = dataPointBufferRef.current;
    buf.count += 1;
    if (pointCode) buf.pointCodes.add(pointCode);
  };

  const lastWsStatusRef = useRef<string | null>(null);
  const handleWsStatusChange = (status: string) => {
    if (lastWsStatusRef.current === status) return;
    lastWsStatusRef.current = status;
    let level: LogEntry['level'] = 'info';
    if (status === 'error' || status === 'failed') level = 'error';
    else if (status === 'stopping' || status === 'paused') level = 'warning';
    else if (status === 'running') level = 'success';
    addLog(level, `会话状态变更为: ${status}`);
  };

  // Connection lifecycle events (connecting / connected / read_failed /
  // disconnected / reconnecting / reconnected / gave_up). These are pushed
  // immediately and DO NOT participate in the 1Hz data_point aggregation.
  const handleWsConnectionEvent = (d: ConnectionEventPayload) => {
    const deviceLabel = d.device_code ? `[${d.device_code}]` : '';
    let level: LogEntry['level'] = 'info';
    let logMessage = '';

    switch (d.event) {
      case 'connecting':
        level = 'info';
        logMessage = `🔌 ${deviceLabel} 正在连接...`;
        break;
      case 'connected':
        level = 'success';
        logMessage = `✅ ${deviceLabel} 连接成功${
          typeof d.connect_duration_ms === 'number'
            ? ` (耗时 ${d.connect_duration_ms}ms)`
            : ''
        }`;
        break;
      case 'read_failed':
        level = 'warning';
        logMessage = `⚠️ ${deviceLabel} 读取失败 ${d.count ?? 0} 次${
          d.last_error ? `（${d.last_error}）` : ''
        }`;
        break;
      case 'disconnected':
        level = 'error';
        logMessage = `🔻 ${deviceLabel} 连接已断开（${d.reason || 'unknown'}${
          typeof d.after_seconds === 'number'
            ? `，已持续 ${d.after_seconds}s 无响应`
            : ''
        }）`;
        break;
      case 'reconnecting':
        level = 'warning';
        logMessage = `🔄 ${deviceLabel} 第 ${d.attempt ?? '?'}/${
          d.max_attempts ?? '?'
        } 次重连...`;
        break;
      case 'reconnected':
        level = 'success';
        logMessage = `✅ ${deviceLabel} 重连成功${
          typeof d.downtime_seconds === 'number'
            ? `（断线 ${d.downtime_seconds}s）`
            : ''
        }`;
        break;
      case 'gave_up':
        level = 'error';
        logMessage = `❌ ${deviceLabel} 重连失败${
          typeof d.will_retry_in_seconds === 'number'
            ? `，${d.will_retry_in_seconds}s 后再试`
            : ''
        }`;
        break;
      default:
        return;
    }

    addLog(level, logMessage, d.timestamp);
  };

  const handleStart = async () => {
    setLoading(true);
    setError(null);
    setSuccess(null);

    try {
      addLog('info', `正在启动任务 ${task.name}...`);

      const result = await startTask({ task_id: task.id });

      if (result.validation) {
        if (result.validation.all_healthy) {
          addLog('success', `任务启动成功，${result.validation.total_points} 个测点就绪`);
          setSuccess(result.detail || '任务启动成功');
        } else {
          const failedCount = result.validation.failed_points_count || 0;
          addLog('warning', `任务已启动但有 ${failedCount} 个测点异常`);
          setSuccess(result.detail || '任务已启动但部分测点异常');
        }

        // Log device-level results
        if (result.validation.device_results) {
          Object.entries(result.validation.device_results).forEach(([deviceCode, deviceResult]: [string, any]) => {
            if (deviceResult.status === 'error') {
              addLog('error', `设备 ${deviceCode} 连接失败: ${deviceResult.error}`);
            } else if (deviceResult.status === 'partial') {
              addLog('warning', `设备 ${deviceCode} 部分测点读取失败`);
            } else {
              addLog('success', `设备 ${deviceCode} 连接正常`);
            }
          });
        }
      } else {
        addLog('success', result.detail || '任务启动成功');
        setSuccess(result.detail || '任务启动成功');
      }

      setTimeout(() => {
        onStatusChange();
      }, 1500);
    } catch (err) {
      const errorMessage = (err as Error).message;
      addLog('error', `启动失败: ${errorMessage}`);
      setError(errorMessage);
    } finally {
      setLoading(false);
    }
  };

  const handleStop = async () => {
    if (!activeSession) return;

    setLoading(true);
    setError(null);
    setSuccess(null);

    try {
      addLog('info', `正在停止会话 #${activeSession.id}...`);

      const result = await stopSession(activeSession.id, '用户手动停止');

      addLog('success', `停止指令已发送: ${result.detail}`);
      setSuccess(result.detail || '任务停止成功');

      setTimeout(() => {
        onStatusChange();
      }, 1500);
    } catch (err) {
      const errorMessage = (err as Error).message;
      addLog('error', `停止失败: ${errorMessage}`);
      setError(errorMessage);
    } finally {
      setLoading(false);
    }
  };

  const handleSaveSampleRate = async () => {
    if (
      sampleRateValue === null ||
      Number.isNaN(sampleRateValue) ||
      sampleRateValue < 0.1 ||
      sampleRateValue > 100
    ) {
      message.error('采样频率需在 0.1 ~ 100 Hz 之间');
      return;
    }

    setSampleRateSaving(true);
    try {
      const updated = await updateTaskSampleRate(task.id, sampleRateValue);
      const newRate =
        typeof updated.sample_rate_hz === 'number'
          ? updated.sample_rate_hz
          : sampleRateValue;
      setSavedSampleRate(newRate);
      setSampleRateValue(newRate);
      addLog('success', `采样频率已更新为 ${newRate} Hz`);
      if (isRunning) {
        message.warning('采样频率已更新，正在重启会话...');
      } else {
        message.success('采样频率已更新，将在下次启动会话时生效');
      }
    } catch (err) {
      const errorMessage = (err as Error).message;
      addLog('error', `更新采样频率失败: ${errorMessage}`);
      message.error(errorMessage);
    } finally {
      setSampleRateSaving(false);
    }
  };

  const formatDuration = (seconds: number | null) => {
    if (!seconds) return '-';
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    return `${hours}h ${minutes}m ${secs}s`;
  };

  const getDeviceHealth = () => {
    if (!activeSession || !activeSession.metadata) return null;
    const deviceHealth = activeSession.metadata.device_health;
    if (!deviceHealth || typeof deviceHealth !== 'object') return null;
    const devices = Object.values(deviceHealth);
    return devices.length > 0 ? devices[0] : null;
  };

  const deviceHealth = getDeviceHealth();

  const getStatusClass = (status: string) => {
    switch (status.toLowerCase()) {
      case 'running':
        return 'status-badge status-badge--running';
      case 'success':
      case 'completed':
        return 'status-badge status-badge--success';
      case 'error':
      case 'failed':
        return 'status-badge status-badge--error';
      case 'starting':
      case 'paused':
        return 'status-badge status-badge--warning';
      case 'stopping':
        return 'status-badge status-badge--warning';
      default:
        return 'status-badge status-badge--stopped';
    }
  };

  const getHealthBadgeClass = (status: string) => {
    switch (status) {
      case 'healthy':
        return 'badge badge--success';
      case 'error':
        return 'badge badge--error';
      case 'timeout':
        return 'badge badge--warning';
      default:
        return 'badge badge--muted';
    }
  };

  const getLogIcon = (level: LogEntry['level']) => {
    switch (level) {
      case 'error':
        return (
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="8" x2="12" y2="12" />
            <line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
        );
      case 'warning':
        return (
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
            <line x1="12" y1="9" x2="12" y2="13" />
            <line x1="12" y1="17" x2="12.01" y2="17" />
          </svg>
        );
      case 'success':
        return (
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
            <polyline points="22 4 12 14.01 9 11.01" />
          </svg>
        );
      default:
        return (
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="16" x2="12" y2="12" />
            <line x1="12" y1="8" x2="12.01" y2="8" />
          </svg>
        );
    }
  };

  return (
    <div className="task-panel">
      {isRunning && activeSession && (
        <SessionEventSubscriber
          key={`ws-${activeSession.id}`}
          sessionId={activeSession.id}
          onDataPoint={handleWsDataPoint}
          onStatusChange={handleWsStatusChange}
          onConnectionEvent={handleWsConnectionEvent}
        />
      )}
      <div className="task-panel__header">
        <div className="task-panel__info">
          <div className="task-panel__title-row">
            <h3 className="task-panel__title">
              <span className={`status-dot ${isRunning ? 'status-dot--running' : 'status-dot--stopped'}`} />
              {task.name}
            </h3>
            <div className="task-panel__badges">
              <span className={task.is_active ? 'badge badge--active' : 'badge badge--muted'}>
                {task.is_active ? '启用' : '停用'}
              </span>
              {deviceHealth && (
                <span className={getHealthBadgeClass(deviceHealth.status)}>
                  设备: {deviceHealth.status === 'healthy' ? '正常' :
                         deviceHealth.status === 'error' ? '错误' :
                         deviceHealth.status === 'timeout' ? '超时' : '断开'}
                </span>
              )}
            </div>
          </div>
          <p className="task-panel__code">{task.code}</p>
          <p className="task-panel__description">{task.description}</p>
        </div>
        <div className="task-panel__actions">
          {isRunning ? (
            <button
              onClick={handleStop}
              disabled={loading}
              className="btn btn--danger"
            >
              {loading ? '停止中...' : '停止'}
            </button>
          ) : (
            <button
              onClick={handleStart}
              disabled={loading || !task.is_active}
              className="btn btn--success"
            >
              {loading ? '启动中...' : '启动'}
            </button>
          )}
          <button
            onClick={() => setShowLogs(!showLogs)}
            className={`btn btn--secondary btn--icon ${showLogs ? 'btn--active' : ''}`}
            title="查看日志"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
              <polyline points="14 2 14 8 20 8" />
              <line x1="16" y1="13" x2="8" y2="13" />
              <line x1="16" y1="17" x2="8" y2="17" />
              <polyline points="10 9 9 9 8 9" />
            </svg>
          </button>
        </div>
      </div>

      <div className="task-panel__sample-rate">
        <div className="sample-rate__row">
          <span className="sample-rate__label">采样频率</span>
          <InputNumber
            min={0.1}
            max={100}
            step={0.1}
            precision={1}
            value={sampleRateValue}
            onChange={(val) => setSampleRateValue(val as number | null)}
            addonAfter="Hz"
            disabled={sampleRateSaving}
            style={{ width: 140 }}
          />
          <Button
            type="primary"
            size="small"
            loading={sampleRateSaving}
            disabled={!sampleRateDirty || sampleRateSaving}
            onClick={handleSaveSampleRate}
          >
            保存
          </Button>
          <span className="sample-rate__hint">
            {isRunning
              ? '※ 修改后将重启采集会话'
              : '※ 修改后将影响新启动的会话'}
          </span>
        </div>
      </div>

      {activeSession && (
        <div className="task-panel__session">
          <div className="session-grid">
            <div className="session-item">
              <span className="session-item__label">会话状态</span>
              <span className={getStatusClass(activeSession.status)}>
                <span className="status-badge__dot" />
                {activeSession.status}
              </span>
            </div>
            <div className="session-item">
              <span className="session-item__label">会话ID</span>
              <span className="session-item__value">#{activeSession.id}</span>
            </div>
            <div className="session-item">
              <span className="session-item__label">开始时间</span>
              <span className="session-item__value">
                {activeSession.started_at
                  ? new Date(activeSession.started_at).toLocaleString('zh-CN')
                  : '-'}
              </span>
            </div>
            <div className="session-item">
              <span className="session-item__label">运行时长</span>
              <span className="session-item__value">{formatDuration(activeSession.duration_seconds)}</span>
            </div>
            {activeSession.error_message && (
              <div className="session-item session-item--error">
                <span className="session-item__label">错误信息</span>
                <span className="session-item__value session-item__value--error">{activeSession.error_message}</span>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Logs Panel */}
      {showLogs && (
        <div className="task-panel__logs">
          <div className="logs-header">
            <span className="logs-title">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                <polyline points="14 2 14 8 20 8" />
              </svg>
              操作日志
            </span>
            <button onClick={clearLogs} className="logs-clear">
              清空
            </button>
          </div>
          <div className="logs-list">
            {logs.length === 0 ? (
              <div className="logs-empty">暂无日志记录</div>
            ) : (
              logs.map((log, index) => (
                <div key={index} className={`log-entry log-entry--${log.level}`}>
                  <span className="log-icon">{getLogIcon(log.level)}</span>
                  <span className="log-time">
                    {new Date(log.timestamp).toLocaleTimeString('zh-CN')}
                  </span>
                  <span className="log-message">{log.message}</span>
                </div>
              ))
            )}
          </div>
        </div>
      )}

      {error && (
        <div className="task-panel__alert task-panel__alert--error">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="8" x2="12" y2="12" />
            <line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
          <span>{error}</span>
        </div>
      )}

      {success && (
        <div className="task-panel__alert task-panel__alert--success">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
            <polyline points="22 4 12 14.01 9 11.01" />
          </svg>
          <span>{success}</span>
        </div>
      )}
    </div>
  );
};

export default TaskControlPanel;
