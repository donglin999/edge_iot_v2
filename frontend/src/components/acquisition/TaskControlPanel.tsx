import { useEffect, useRef, useState } from 'react';
import {
  Alert,
  App,
  Badge,
  Button,
  Card,
  Descriptions,
  InputNumber,
  List,
  Space,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  ExclamationCircleOutlined,
  FileTextOutlined,
  InfoCircleOutlined,
} from '@ant-design/icons';
import type { AcqTask, AcquisitionSession } from '../../services/acquisitionApi';
import {
  startTask,
  stopSession,
  updateTaskSampleRate,
} from '../../services/acquisitionApi';
import { deleteTask } from '../../services/taskApi';
import { useWebSocket, WebSocketMessage } from '../../hooks/useWebSocket';
import { buildWebSocketUrl } from '../../services/apiClient';

const { Text } = Typography;

interface TaskControlPanelProps {
  task: AcqTask;
  activeSession?: AcquisitionSession;
  onStatusChange: () => void;
  /** 打开「编辑任务」弹窗(改名 / 测点增删改 / 频率)。 */
  onEdit?: () => void;
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
  onEdit,
}) => {
  // antd5:静态 message/Modal.confirm 拿不到 ConfigProvider 的自定义 theme,
  // 会在控制台刷 "Static function can not consume context" 警告 —— 改用
  // App.useApp() 拿 context-aware 的实例(App.tsx 的 <AntdApp> 已经包了)。
  const { message, modal } = App.useApp();
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

  // 入库速率的展示已按用户要求移除(2026-07-24)。后端仍在 session.metadata 里
  // 维护 ingest_points_per_sec / ingest_target_points_per_sec,作为诊断数据可经
  // /acquisition/sessions/{id}/status/ 查看 —— 只是不再上界面。

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
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
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

  const handleDelete = () => {
    modal.confirm({
      title: `删除任务「${task.name}」?`,
      content: isRunning
        ? '该任务正在采集中,会先停止再删除。任务下的测点会一并删除,但不影响设备本身。'
        : '任务下的测点会一并删除,但不影响设备本身。此操作不可撤销。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          if (isRunning && activeSession) {
            await stopSession(activeSession.id, '删除任务前停止').catch(() => undefined);
          }
          await deleteTask(task.id);
          message.success('任务已删除');
          onStatusChange();
        } catch (err) {
          message.error(`删除失败: ${(err as Error).message}`);
        }
      },
    });
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

  // 会话状态 → antd Badge 的 status 语义色(带小圆点,和原 .status-badge__dot
  // 是同一个视觉意图)。running 沿用原设计的琥珀色,不是常见的"运行=绿色"。
  const getStatusBadge = (status: string): 'success' | 'error' | 'warning' | 'default' => {
    switch (status.toLowerCase()) {
      case 'running':
        return 'warning';
      case 'success':
      case 'succeeded':
      case 'completed':
        return 'success';
      case 'error':
      case 'failed':
        return 'error';
      case 'starting':
      case 'paused':
      case 'stopping':
        return 'warning';
      default:
        return 'default';
    }
  };

  // 会话状态中文化。以前直接渲染 activeSession.status 原值(英文,如
  // "running"/"stopped"),且 'succeeded' 这种非枚举内取值会落到
  // getStatusClass 的 default 分支变灰(rank22d,思路同 DashboardPage
  // 的 RUN_STATUS 映射)。
  const SESSION_STATUS_LABEL: Record<string, string> = {
    starting: '启动中',
    running: '运行中',
    paused: '已暂停',
    stopping: '停止中',
    stopped: '已停止',
    error: '错误',
    success: '成功',
    succeeded: '成功',
    completed: '完成',
    failed: '失败',
  };
  const getStatusLabel = (status: string) => SESSION_STATUS_LABEL[status?.toLowerCase()] ?? status;

  const getHealthTagColor = (status: string) => {
    switch (status) {
      case 'healthy':
        return 'success';
      case 'error':
        return 'error';
      case 'timeout':
        return 'warning';
      default:
        return 'default';
    }
  };

  const LOG_ICON: Record<LogEntry['level'], React.ReactNode> = {
    error: <CloseCircleOutlined style={{ color: 'var(--error, #ef4444)' }} />,
    warning: <ExclamationCircleOutlined style={{ color: 'var(--warning, #f59e0b)' }} />,
    success: <CheckCircleOutlined style={{ color: 'var(--success, #22c55e)' }} />,
    info: <InfoCircleOutlined style={{ color: 'var(--info, #3b82f6)' }} />,
  };

  const logLevelColor: Record<LogEntry['level'], string | undefined> = {
    error: 'var(--error, #ef4444)',
    warning: 'var(--warning, #f59e0b)',
    success: undefined,
    info: undefined,
  };

  return (
    <Card size="small">
      {isRunning && activeSession && (
        <SessionEventSubscriber
          key={`ws-${activeSession.id}`}
          sessionId={activeSession.id}
          onDataPoint={handleWsDataPoint}
          onStatusChange={handleWsStatusChange}
          onConnectionEvent={handleWsConnectionEvent}
        />
      )}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 20 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', marginBottom: 8 }}>
            <Text strong style={{ fontSize: 16, display: 'flex', alignItems: 'center', gap: 8 }}>
              <Badge status={isRunning ? 'success' : 'default'} />
              {task.name}
            </Text>
            <Space size={4}>
              <Tag color={task.is_active ? 'success' : 'default'}>{task.is_active ? '启用' : '停用'}</Tag>
              {deviceHealth && (
                <Tag color={getHealthTagColor(deviceHealth.status)}>
                  设备: {deviceHealth.status === 'healthy' ? '正常' :
                         deviceHealth.status === 'error' ? '错误' :
                         deviceHealth.status === 'timeout' ? '超时' : '断开'}
                </Tag>
              )}
            </Space>
          </div>
          <Text type="secondary" style={{ fontFamily: 'monospace', fontSize: 13, display: 'block' }}>
            {task.code}
          </Text>
          <Text type="secondary" style={{ fontSize: 14 }}>{task.description}</Text>
        </div>
        <Space>
          {isRunning ? (
            <Button danger loading={loading} onClick={handleStop}>
              {loading ? '停止中...' : '停止'}
            </Button>
          ) : (
            <Button
              type="primary"
              loading={loading}
              disabled={!task.is_active}
              onClick={handleStart}
            >
              {loading ? '启动中...' : '启动'}
            </Button>
          )}
          <Tooltip title="查看日志">
            <Button
              type={showLogs ? 'primary' : 'default'}
              icon={<FileTextOutlined />}
              onClick={() => setShowLogs(!showLogs)}
            />
          </Tooltip>
          {onEdit && (
            <Tooltip title="编辑任务(改名/测点/频率)">
              <Button icon={<EditOutlined />} onClick={onEdit} />
            </Tooltip>
          )}
          <Tooltip title="删除任务">
            <Button danger icon={<DeleteOutlined />} onClick={handleDelete} />
          </Tooltip>
        </Space>
      </div>

      <div style={{ marginTop: 16, paddingTop: 16, borderTop: '1px solid var(--border-primary, #e5e7eb)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <Text type="secondary" style={{ fontSize: 13, fontWeight: 500, minWidth: 72 }}>
            采样频率
          </Text>
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
          <Text type="secondary" style={{ fontSize: 12 }}>
            {isRunning
              ? '※ 保存后立即以新频率重启采集'
              : '※ 修改后将影响新启动的会话'}
          </Text>
        </div>
      </div>

      {activeSession && (
        <div style={{ marginTop: 16, paddingTop: 16, borderTop: '1px solid var(--border-primary, #e5e7eb)' }}>
          <Descriptions size="small" column={{ xs: 1, sm: 2, md: 4 }}>
            <Descriptions.Item label="会话状态">
              <Badge status={getStatusBadge(activeSession.status)} text={getStatusLabel(activeSession.status)} />
            </Descriptions.Item>
            <Descriptions.Item label="会话ID">#{activeSession.id}</Descriptions.Item>
            <Descriptions.Item label="开始时间">
              {activeSession.started_at
                ? new Date(activeSession.started_at).toLocaleString('zh-CN')
                : '-'}
            </Descriptions.Item>
            <Descriptions.Item label="运行时长">
              {formatDuration(activeSession.duration_seconds)}
            </Descriptions.Item>
            {activeSession.error_message && (
              <Descriptions.Item label="错误信息" span={2}>
                <Text type="danger">{activeSession.error_message}</Text>
              </Descriptions.Item>
            )}
          </Descriptions>
        </div>
      )}

      {/* Logs Panel */}
      {showLogs && (
        <div style={{ marginTop: 16 }}>
          <List
            size="small"
            bordered
            header={
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <Text strong style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
                  <FileTextOutlined /> 操作日志
                </Text>
                <Button size="small" type="text" onClick={clearLogs}>清空</Button>
              </div>
            }
            dataSource={logs}
            locale={{ emptyText: '暂无日志记录' }}
            style={{ maxHeight: 240, overflowY: 'auto' }}
            renderItem={(log, index) => (
              <List.Item key={index}>
                <Space align="start" size={8} style={{ width: '100%' }}>
                  {LOG_ICON[log.level]}
                  <Text type="secondary" style={{ fontSize: 11, fontFamily: 'monospace', whiteSpace: 'nowrap' }}>
                    {new Date(log.timestamp).toLocaleTimeString('zh-CN')}
                  </Text>
                  <Text style={{ fontSize: 13, color: logLevelColor[log.level], wordBreak: 'break-word' }}>
                    {log.message}
                  </Text>
                </Space>
              </List.Item>
            )}
          />
        </div>
      )}

      {error && (
        <Alert type="error" showIcon message={error} style={{ marginTop: 16 }} />
      )}

      {success && (
        <Alert type="success" showIcon message={success} style={{ marginTop: 16 }} />
      )}
    </Card>
  );
};

export default TaskControlPanel;
