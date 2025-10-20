import { useState } from 'react';
import type { AcqTask, AcquisitionSession } from '../../services/acquisitionApi';
import { startTask, stopSession } from '../../services/acquisitionApi';

interface TaskControlPanelProps {
  task: AcqTask;
  activeSession?: AcquisitionSession;
  onStatusChange: () => void;
}

const TaskControlPanel: React.FC<TaskControlPanelProps> = ({
  task,
  activeSession,
  onStatusChange,
}) => {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const isRunning = activeSession && activeSession.status === 'running';

  const handleStart = async () => {
    setLoading(true);
    setError(null);
    setSuccess(null);

    try {
      const result = await startTask({ task_id: task.id });
      setSuccess(result.detail || '任务启动成功');
      setTimeout(() => {
        onStatusChange();
      }, 1000);
    } catch (err) {
      setError((err as Error).message);
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
      const result = await stopSession(activeSession.id, '用户手动停止');
      setSuccess(result.detail || '任务停止成功');
      setTimeout(() => {
        onStatusChange();
      }, 1000);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const formatDuration = (seconds: number | null) => {
    if (!seconds) return '-';
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    return `${hours}h ${minutes}m ${secs}s`;
  };

  // 获取设备健康状态
  const getDeviceHealth = () => {
    if (!activeSession || !activeSession.metadata) return null;
    const deviceHealth = activeSession.metadata.device_health;
    if (!deviceHealth || typeof deviceHealth !== 'object') return null;

    // 从device_health对象中获取第一个设备的状态
    const devices = Object.values(deviceHealth);
    return devices.length > 0 ? devices[0] : null;
  };

  const deviceHealth = getDeviceHealth();

  return (
    <div className="task-control-panel">
      <div className="task-header">
        <div className="task-info">
          <div className="task-title-row">
            <h3>
              <span className={`status-indicator status-${isRunning ? 'running' : 'stopped'}`}>
                {isRunning ? '●' : '○'}
              </span>
              {task.name}
            </h3>
            <div className="task-badges">
              <span className={`badge badge-${task.is_active ? 'enabled' : 'disabled'}`}>
                {task.is_active ? '启用' : '停用'}
              </span>
              {deviceHealth && (
                <span className={`badge badge-device-${deviceHealth.status}`}>
                  设备状态: {deviceHealth.status === 'healthy' ? '正常' :
                            deviceHealth.status === 'error' ? '错误' :
                            deviceHealth.status === 'timeout' ? '超时' : '断开'}
                </span>
              )}
            </div>
          </div>
          <p className="task-code">任务编码: {task.code}</p>
          <p className="task-description">{task.description}</p>
        </div>
        <div className="task-actions">
          {isRunning ? (
            <button
              onClick={handleStop}
              disabled={loading}
              className="btn btn-danger"
            >
              {loading ? '停止中...' : '停止'}
            </button>
          ) : (
            <button
              onClick={handleStart}
              disabled={loading || !task.is_active}
              className="btn btn-primary"
            >
              {loading ? '启动中...' : '启动'}
            </button>
          )}
        </div>
      </div>

      {activeSession && (
        <div className="session-info">
          <div className="info-item">
            <span className="label">状态:</span>
            <span className={`value status-${activeSession.status}`}>
              {activeSession.status}
            </span>
          </div>
          <div className="info-item">
            <span className="label">会话ID:</span>
            <span className="value">{activeSession.id}</span>
          </div>
          <div className="info-item">
            <span className="label">开始时间:</span>
            <span className="value">
              {activeSession.started_at
                ? new Date(activeSession.started_at).toLocaleString('zh-CN')
                : '-'}
            </span>
          </div>
          <div className="info-item">
            <span className="label">运行时长:</span>
            <span className="value">{formatDuration(activeSession.duration_seconds)}</span>
          </div>
          {activeSession.error_message && (
            <div className="info-item error">
              <span className="label">错误:</span>
              <span className="value">{activeSession.error_message}</span>
            </div>
          )}
        </div>
      )}

      {error && <div className="alert alert-error">{error}</div>}
      {success && <div className="alert alert-success">{success}</div>}

      <style>{`
        .task-control-panel {
          border: 1px solid #e0e0e0;
          border-radius: 8px;
          padding: 1.5rem;
          margin-bottom: 1rem;
          background: white;
        }

        .task-header {
          display: flex;
          justify-content: space-between;
          align-items: flex-start;
          margin-bottom: 1rem;
        }

        .task-info {
          flex: 1;
        }

        .task-title-row {
          display: flex;
          align-items: center;
          gap: 1rem;
          margin-bottom: 0.5rem;
        }

        .task-header h3 {
          margin: 0;
          display: flex;
          align-items: center;
          gap: 0.5rem;
        }

        .task-badges {
          display: flex;
          gap: 0.5rem;
        }

        .badge {
          padding: 0.25rem 0.75rem;
          border-radius: 12px;
          font-size: 0.75rem;
          font-weight: 500;
        }

        .badge-enabled {
          background: #e8f5e9;
          color: #2e7d32;
        }

        .badge-disabled {
          background: #f5f5f5;
          color: #999;
        }

        .badge-device-healthy {
          background: #e3f2fd;
          color: #1976d2;
        }

        .badge-device-error {
          background: #ffebee;
          color: #c62828;
        }

        .badge-device-timeout {
          background: #fff3e0;
          color: #ef6c00;
        }

        .badge-device-disconnected {
          background: #f5f5f5;
          color: #666;
        }

        .task-code {
          margin: 0 0 0.25rem 0;
          color: #999;
          font-size: 0.85rem;
          font-family: monospace;
        }

        .task-description {
          margin: 0;
          color: #666;
          font-size: 0.9rem;
        }

        .status-indicator {
          font-size: 1.2rem;
        }

        .status-running {
          color: #4caf50;
        }

        .status-stopped {
          color: #999;
        }

        .task-actions {
          display: flex;
          gap: 0.5rem;
        }

        .session-info {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
          gap: 1rem;
          padding: 1rem;
          background: #f5f5f5;
          border-radius: 4px;
          margin-top: 1rem;
        }

        .info-item {
          display: flex;
          flex-direction: column;
          gap: 0.25rem;
        }

        .info-item .label {
          font-size: 0.85rem;
          color: #666;
        }

        .info-item .value {
          font-weight: 500;
        }

        .info-item.error .value {
          color: #f44336;
        }

        .status-starting { color: #ff9800; }
        .status-running { color: #4caf50; }
        .status-paused { color: #2196f3; }
        .status-stopping { color: #ff9800; }
        .status-stopped { color: #999; }
        .status-error { color: #f44336; }

        .alert {
          margin-top: 1rem;
          padding: 0.75rem 1rem;
          border-radius: 4px;
        }

        .alert-error {
          background: #ffebee;
          color: #c62828;
          border: 1px solid #ef5350;
        }

        .alert-success {
          background: #e8f5e9;
          color: #2e7d32;
          border: 1px solid #66bb6a;
        }

        .btn {
          padding: 0.5rem 1.5rem;
          border: none;
          border-radius: 4px;
          cursor: pointer;
          font-size: 0.9rem;
          font-weight: 500;
          transition: all 0.2s;
        }

        .btn:disabled {
          opacity: 0.5;
          cursor: not-allowed;
        }

        .btn-primary {
          background: #2196f3;
          color: white;
        }

        .btn-primary:hover:not(:disabled) {
          background: #1976d2;
        }

        .btn-danger {
          background: #f44336;
          color: white;
        }

        .btn-danger:hover:not(:disabled) {
          background: #d32f2f;
        }
      `}</style>
    </div>
  );
};

export default TaskControlPanel;
