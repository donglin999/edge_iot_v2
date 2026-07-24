import { useEffect, useState, useCallback } from 'react';
import type { CSSProperties } from 'react';
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Collapse,
  Empty,
  Row,
  Spin,
  Statistic,
  Switch,
  Tooltip,
  Typography,
} from 'antd';
import {
  CheckCircleOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  UnorderedListOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import type { AcqTask, AcquisitionSession } from '../services/acquisitionApi';
import { fetchTasks, fetchActiveSessions } from '../services/acquisitionApi';
import { isAbortError } from '../services/http';
import TaskControlPanel from '../components/acquisition/TaskControlPanel';
import TaskFormModal from '../components/acquisition/TaskFormModal';
import { useWebSocket, WebSocketStatus, WebSocketMessage } from '../hooks/useWebSocket';

const { Title, Text } = Typography;

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
  // 任务管理并进本页:新建 / 编辑弹窗。editingTaskId===undefined 且 modalOpen 为新建。
  const [taskModalOpen, setTaskModalOpen] = useState(false);
  const [editingTaskId, setEditingTaskId] = useState<number | undefined>(undefined);

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
  // WS 只在**状态变化**时推送,会话 metadata 里的持续量(运行时长、健康摘要等)
  // 状态不变时不会更新 —— 轮询作为兜底让面板数据保持新鲜;WS 仍负责状态变更的
  // 即时反馈,两者都写 activeSessions,幂等无害。
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

  const getWsStatusBadge = () => {
    switch (wsStatus) {
      case WebSocketStatus.CONNECTED:
        return <Badge status="success" text="实时连接已建立" />;
      case WebSocketStatus.CONNECTING:
        return <Badge status="processing" text="连接中..." />;
      case WebSocketStatus.DISCONNECTED:
        return <Badge status="error" text="实时连接已断开" />;
      case WebSocketStatus.ERROR:
        return <Badge status="error" text="连接错误" />;
      default:
        return null;
    }
  };

  if (initialLoading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '60vh' }}>
        <Spin size="large" />
      </div>
    );
  }

  const taskListStyle: CSSProperties = {
    display: 'flex',
    flexDirection: 'column',
    gap: 12,
  };

  return (
    <div style={{ maxWidth: 1200 }}>
      {/* Header */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 24,
          gap: 16,
          flexWrap: 'wrap',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          <Title level={3} style={{ margin: 0 }}>
            采集控制台
          </Title>
          {getWsStatusBadge()}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <Tooltip title="关闭后,采集与会话状态仍会每 3 秒轮询兜底刷新;这个开关只控制状态变更是否通过 WebSocket 即时推送。">
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Switch
                size="small"
                checked={useWebSocketUpdates}
                onChange={setUseWebSocketUpdates}
              />
              <Text type="secondary" style={{ fontSize: 14 }}>
                WebSocket 实时推送(关闭后仍每 3s 轮询兜底)
              </Text>
            </div>
          </Tooltip>
          <Button icon={<ReloadOutlined />} loading={refreshing} onClick={() => loadData()}>
            刷新
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => {
              setEditingTaskId(undefined);
              setTaskModalOpen(true);
            }}
          >
            新建任务
          </Button>
        </div>
      </div>

      {error && (
        <Alert
          type="error"
          showIcon
          message={`加载失败: ${error}`}
          style={{ marginBottom: 20 }}
        />
      )}

      {/* Stats */}
      <Row gutter={16} style={{ marginBottom: 32 }}>
        <Col xs={12} md={6}>
          <Card variant="borderless">
            <Statistic title="任务总数" value={tasks.length} prefix={<UnorderedListOutlined />} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card variant="borderless">
            <Statistic title="启用任务" value={activeTasks.length} prefix={<CheckCircleOutlined />} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card variant="borderless">
            <Statistic
              title="运行中"
              value={runningCount}
              prefix={<PlayCircleOutlined />}
              valueStyle={runningCount > 0 ? { color: '#22c55e' } : undefined}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card variant="borderless">
            <Statistic
              title="错误"
              value={errorCount}
              prefix={<WarningOutlined />}
              valueStyle={errorCount > 0 ? { color: '#ef4444' } : undefined}
            />
          </Card>
        </Col>
      </Row>

      {/* Active Tasks */}
      <div style={{ marginBottom: 24 }}>
        <Title level={4} style={{ marginBottom: 16 }}>
          启用的任务 ({activeTasks.length})
        </Title>
        {activeTasks.length === 0 ? (
          <Empty
            description={
              <>
                <div>暂无激活的采集任务</div>
                <Text type="secondary" style={{ fontSize: 13 }}>
                  导入配置或启用任务以开始数据采集
                </Text>
              </>
            }
          />
        ) : (
          <div style={taskListStyle}>
            {activeTasks.map((task) => (
              <TaskControlPanel
                key={task.id}
                task={task}
                activeSession={getSessionForTask(task.id)}
                onStatusChange={loadData}
                onEdit={() => {
                  setEditingTaskId(task.id);
                  setTaskModalOpen(true);
                }}
              />
            ))}
          </div>
        )}
      </div>

      {/* Inactive Tasks */}
      {inactiveTasks.length > 0 && (
        <Collapse
          ghost
          style={{ marginBottom: 24 }}
          items={[
            {
              key: 'inactive',
              label: (
                <Text strong style={{ fontSize: 16 }}>
                  未激活的任务 ({inactiveTasks.length})
                </Text>
              ),
              children: (
                <div style={taskListStyle}>
                  {inactiveTasks.map((task) => (
                    <TaskControlPanel
                      key={task.id}
                      task={task}
                      activeSession={getSessionForTask(task.id)}
                      onStatusChange={loadData}
                      onEdit={() => {
                        setEditingTaskId(task.id);
                        setTaskModalOpen(true);
                      }}
                    />
                  ))}
                </div>
              ),
            },
          ]}
        />
      )}

      <TaskFormModal
        open={taskModalOpen}
        taskId={editingTaskId}
        onClose={() => setTaskModalOpen(false)}
        onSaved={loadData}
      />
    </div>
  );
};

export default AcquisitionControlPage;
