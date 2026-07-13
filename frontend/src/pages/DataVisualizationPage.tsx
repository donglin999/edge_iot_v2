/**
 * Data Visualization Page
 *
 * Three-level cascading filter (task → device → point) plus two stacked
 * panels:
 *   1. Realtime panel — a flat grid of cards, one per matching point.
 *   2. History panel  — single-point history chart with time-range Segmented
 *      control and CSV export. Empty until a specific point is selected.
 */
import React, { useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Empty,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Statistic,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import {
  ArrowLeftOutlined,
  ArrowRightOutlined,
  DownloadOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import relativeTime from 'dayjs/plugin/relativeTime';
import 'dayjs/locale/zh-cn';

import { AcqTask, fetchTasks } from '../services/acquisitionApi';
import {
  PointLatestValue,
  fetchPointHistory,
  fetchPointsLatestValues,
} from '../services/dataApi';
import { isAbortError } from '../services/http';
import PointChart from '../components/PointChart';
import VirtualPointGrid from '../components/VirtualPointGrid';
import './DataVisualizationPage.css';

dayjs.extend(relativeTime);
dayjs.locale('zh-cn');

const { Title, Text } = Typography;

type RefreshInterval = 1000 | 3000 | 5000;
type HistoryRange = '5m' | '1h' | '6h' | '24h';

interface Filter {
  taskId: number | null;
  deviceId: number | null;
  pointCode: string | null;
}

const REFRESH_OPTIONS: Array<{ label: string; value: RefreshInterval }> = [
  { label: '1s', value: 1000 },
  { label: '3s', value: 3000 },
  { label: '5s', value: 5000 },
];

const HISTORY_OPTIONS: Array<{ label: string; value: HistoryRange }> = [
  { label: '近 5 分钟', value: '5m' },
  { label: '近 1 小时', value: '1h' },
  { label: '近 6 小时', value: '6h' },
  { label: '近 24 小时', value: '24h' },
];

// Above this many matching points the realtime panel switches to a
// virtualized grid so the DOM stays bounded (M11 in XIU-7).
const VIRTUALIZE_THRESHOLD = 120;

const RANGE_TO_MS: Record<HistoryRange, number> = {
  '5m': 5 * 60 * 1000,
  '1h': 60 * 60 * 1000,
  '6h': 6 * 60 * 60 * 1000,
  '24h': 24 * 60 * 60 * 1000,
};

const qualityToBadge = (quality: string) => {
  switch ((quality || '').toLowerCase()) {
    case 'good':
      return { status: 'success' as const, label: '良好' };
    case 'bad':
      return { status: 'error' as const, label: '错误' };
    case 'uncertain':
      return { status: 'warning' as const, label: '不确定' };
    default:
      return { status: 'default' as const, label: quality || '未知' };
  }
};

const formatValue = (value: PointLatestValue['value']): string => {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'number') {
    if (Number.isInteger(value)) return value.toString();
    return value.toFixed(3);
  }
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  return String(value);
};

const formatRelative = (iso: string | null, now: number): string => {
  if (!iso) return '从未更新';
  const dt = dayjs(iso);
  if (!dt.isValid()) return '从未更新';
  // Use `now` to ensure component re-renders refresh the relative time.
  const ms = now - dt.valueOf();
  if (ms < 0) return '刚刚';
  if (ms < 60_000) return `${Math.max(1, Math.floor(ms / 1000))} 秒前更新`;
  return `${dt.fromNow(true)}前更新`;
};

// A single 1s clock lives in this provider. Only components that consume the
// context (the small <RelativeTime> labels) re-render each tick — the page and
// the memoized point cards do not, because their element identity is unchanged
// when the provider re-renders its `children`.
const NowContext = React.createContext<number>(Date.now());

const NowProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  return <NowContext.Provider value={now}>{children}</NowContext.Provider>;
};

// Isolated ticking subtree: re-renders once per second on its own, without
// dragging the enclosing card (or the whole page) into the re-render.
const RelativeTime: React.FC<{ iso: string | null }> = ({ iso }) => {
  const now = useContext(NowContext);
  return <>{formatRelative(iso, now)}</>;
};

const DataVisualizationPage: React.FC = () => {
  const navigate = useNavigate();

  const [filter, setFilter] = useState<Filter>({
    taskId: null,
    deviceId: null,
    pointCode: null,
  });
  const [pollInterval, setPollInterval] = useState<RefreshInterval>(3000);
  const [tasks, setTasks] = useState<AcqTask[]>([]);
  const [tasksLoading, setTasksLoading] = useState(true);
  const [latestValues, setLatestValues] = useState<PointLatestValue[]>([]);
  // Task-scoped dataset (filtered ONLY by the selected task, never by the
  // device/point selection). The device & point dropdown option lists are
  // derived from this so picking a device doesn't collapse the device list.
  const [taskScopedValues, setTaskScopedValues] = useState<PointLatestValue[]>([]);
  const [latestUpdatedAt, setLatestUpdatedAt] = useState<number | null>(null);
  const [latestError, setLatestError] = useState<string | null>(null);
  const [latestLoading, setLatestLoading] = useState(false);
  const [historyRange, setHistoryRange] = useState<HistoryRange>('1h');
  const [historyRefreshKey, setHistoryRefreshKey] = useState(0);
  const [exporting, setExporting] = useState(false);

  // Use a ref so changing pollInterval doesn't kick off duplicate timers
  // mid-flight. The polling effect re-runs only on filter or interval change.
  const filterRef = useRef(filter);
  filterRef.current = filter;

  // Tasks (top filter level).
  useEffect(() => {
    const aborter = new AbortController();
    fetchTasks(aborter.signal)
      .then((list) => {
        if (!aborter.signal.aborted) setTasks(list);
      })
      .catch((err) => {
        // Surface as latestError; user can still see the empty state.
        if (!isAbortError(err)) setLatestError((err as Error).message);
      })
      .finally(() => {
        if (!aborter.signal.aborted) setTasksLoading(false);
      });
    return () => aborter.abort();
  }, []);

  // Latest-value fetch — shared by the filter effect, poll timer and manual
  // refresh. Threads an AbortSignal so each caller can cancel its request.
  const fetchLatest = useCallback(
    async (current: Filter, signal?: AbortSignal) => {
      const res = await fetchPointsLatestValues(
        {
          taskId: current.taskId,
          deviceId: current.deviceId,
          pointCode: current.pointCode,
        },
        signal,
      );
      return res.points || [];
    },
    [],
  );

  const applyLatest = useCallback((points: PointLatestValue[]) => {
    setLatestValues(points);
    setLatestUpdatedAt(Date.now());
    setLatestError(null);
  }, []);

  // Immediate refetch whenever the filter changes. Kept separate from the
  // poll timer below so a filter change does NOT tear down / restart the
  // interval — only `pollInterval` controls the timer's lifecycle.
  useEffect(() => {
    const aborter = new AbortController();
    setLatestLoading(true);
    fetchLatest(filter, aborter.signal)
      .then((points) => {
        if (!aborter.signal.aborted) applyLatest(points);
      })
      .catch((err) => {
        if (!isAbortError(err)) setLatestError((err as Error).message);
      })
      .finally(() => {
        if (!aborter.signal.aborted) setLatestLoading(false);
      });
    return () => aborter.abort();
  }, [filter, fetchLatest, applyLatest]);

  // Poll timer. Reads the live filter from `filterRef`, so the interval is
  // (re)created only when `pollInterval` changes — never on filter change.
  useEffect(() => {
    let aborter: AbortController | null = null;

    const tick = () => {
      aborter?.abort();
      aborter = new AbortController();
      const { signal } = aborter;
      fetchLatest(filterRef.current, signal)
        .then((points) => {
          if (!signal.aborted) applyLatest(points);
        })
        .catch((err) => {
          if (!isAbortError(err)) setLatestError((err as Error).message);
        });
    };

    const timer = window.setInterval(tick, pollInterval);
    return () => {
      window.clearInterval(timer);
      aborter?.abort();
    };
  }, [pollInterval, fetchLatest, applyLatest]);

  // Task-scoped fetch: refetch the full (device-unfiltered) point set whenever
  // the selected task changes. Feeds the device / point dropdown options so
  // they stay complete regardless of the current device/point selection.
  useEffect(() => {
    const aborter = new AbortController();
    fetchLatest(
      { taskId: filter.taskId, deviceId: null, pointCode: null },
      aborter.signal,
    )
      .then((points) => {
        if (!aborter.signal.aborted) setTaskScopedValues(points);
      })
      .catch((err) => {
        if (!isAbortError(err)) setLatestError((err as Error).message);
      });
    return () => aborter.abort();
  }, [filter.taskId, fetchLatest]);

  const handleManualRefresh = useCallback(() => {
    fetchLatest(filterRef.current)
      .then(applyLatest)
      .catch((err) => {
        if (!isAbortError(err)) setLatestError((err as Error).message);
      });
  }, [fetchLatest, applyLatest]);

  // Cascading-reset helpers. `onPointChange` is memoized so the memoized
  // PointValueCard's onSelect prop stays stable and cards don't re-render.
  const onTaskChange = (value: number | null) => {
    setFilter({ taskId: value ?? null, deviceId: null, pointCode: null });
  };
  const onDeviceChange = (value: number | null) => {
    setFilter((prev) => ({ ...prev, deviceId: value ?? null, pointCode: null }));
  };
  const onPointChange = useCallback((value: string | null) => {
    setFilter((prev) => ({ ...prev, pointCode: value ?? null }));
    setHistoryRefreshKey((n) => n + 1);
  }, []);

  // Cascading options derived from the task-scoped dataset + tasks.
  const taskOptions = useMemo(
    () =>
      tasks
        .filter((t) => t.is_active)
        .map((t) => ({ label: t.name || t.code, value: t.id })),
    [tasks],
  );

  // Device options span the whole task (NOT the current device selection), so
  // the user can freely switch devices without first clearing the filter.
  const deviceOptions = useMemo(() => {
    const seen = new Map<number, string>();
    taskScopedValues.forEach((p) => {
      if (!seen.has(p.device_id)) {
        seen.set(p.device_id, p.device_name || `设备 #${p.device_id}`);
      }
    });
    return Array.from(seen.entries()).map(([id, name]) => ({
      label: name,
      value: id,
    }));
  }, [taskScopedValues]);

  // Point options narrow to the selected device (desirable cascade) but are
  // still sourced from the task-scoped dataset rather than the device-filtered
  // latestValues, so clearing the device restores the full point list.
  const pointOptions = useMemo(() => {
    const source =
      filter.deviceId != null
        ? taskScopedValues.filter((p) => p.device_id === filter.deviceId)
        : taskScopedValues;
    const seen = new Map<string, string>();
    source.forEach((p) => {
      if (!seen.has(p.point_code)) {
        seen.set(p.point_code, p.point_name || p.point_code);
      }
    });
    return Array.from(seen.entries()).map(([code, name]) => ({
      label: `${name} (${code})`,
      value: code,
    }));
  }, [taskScopedValues, filter.deviceId]);

  // Selected point object (for history panel).
  const selectedPoint = useMemo(() => {
    if (!filter.pointCode) return null;
    return latestValues.find((p) => p.point_code === filter.pointCode) ?? null;
  }, [filter.pointCode, latestValues]);

  // History time range — recomputed on range change / refresh key.
  const historyTimeRange = useMemo(() => {
    const end = new Date();
    const start = new Date(end.getTime() - RANGE_TO_MS[historyRange]);
    return { start: start.toISOString(), end: end.toISOString() };
    // historyRefreshKey 故意保留：手动刷新时强制重算"现在"的时间窗口
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [historyRange, historyRefreshKey]);

  const handleExport = async () => {
    if (!filter.pointCode) return;
    setExporting(true);
    try {
      const start = new Date(Date.now() - RANGE_TO_MS[historyRange]).toISOString();
      const end = new Date().toISOString();
      const res = await fetchPointHistory(filter.pointCode, start, end, 10000);
      const headers = ['时间', '数值', '质量'];
      const rows = res.data.map((dp) => [
        new Date(dp.timestamp).toLocaleString('zh-CN'),
        String(dp.value ?? ''),
        dp.quality,
      ]);
      const csv = [headers.join(','), ...rows.map((r) => r.join(','))].join('\n');
      const blob = new Blob([`\uFEFF${csv}`], { type: 'text/csv;charset=utf-8;' });
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = `${filter.pointCode}_${dayjs().format('YYYYMMDD_HHmmss')}.csv`;
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (err) {
      console.error('Export failed:', err);
    } finally {
      setExporting(false);
    }
  };

  const matchingCount = latestValues.length;

  // CTA target for the history empty state — first matching point.
  const ctaPoint = latestValues[0] ?? null;

  return (
    <NowProvider>
    <div className="data-viz-page">
      <div className="data-viz-page__header">
        <div>
          <Title level={3} style={{ margin: 0 }}>
            数据可视化
          </Title>
          <Text type="secondary">
            按 任务 / 设备 / 测点 三级筛选，每 {pollInterval / 1000} 秒自动刷新
          </Text>
        </div>
        <Space>
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/')}>
            返回首页
          </Button>
        </Space>
      </div>

      {/* Filter bar */}
      <Card className="data-viz-page__filterbar" bordered={false}>
        <Row gutter={[12, 12]} align="middle" wrap>
          <Col flex="220px">
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder={tasksLoading ? '加载任务中...' : '任务（全部）'}
              style={{ width: '100%' }}
              value={filter.taskId ?? undefined}
              onChange={(v) => onTaskChange((v as number | undefined) ?? null)}
              options={taskOptions}
              loading={tasksLoading}
              notFoundContent={tasksLoading ? <Spin size="small" /> : '暂无运行中任务'}
            />
          </Col>
          <Col flex="220px">
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="设备（全部）"
              style={{ width: '100%' }}
              value={filter.deviceId ?? undefined}
              onChange={(v) => onDeviceChange((v as number | undefined) ?? null)}
              options={deviceOptions}
              disabled={deviceOptions.length === 0}
            />
          </Col>
          <Col flex="280px">
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="测点（全部）"
              style={{ width: '100%' }}
              value={filter.pointCode ?? undefined}
              onChange={(v) => onPointChange((v as string | undefined) ?? null)}
              options={pointOptions}
              disabled={pointOptions.length === 0}
            />
          </Col>
          <Col flex="auto" />
          <Col>
            <Space size={8}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                刷新间隔
              </Text>
              <Segmented<RefreshInterval>
                options={REFRESH_OPTIONS}
                value={pollInterval}
                onChange={(v) => setPollInterval(v as RefreshInterval)}
              />
              <Tooltip title="立即刷新">
                <Button
                  icon={<ReloadOutlined />}
                  onClick={handleManualRefresh}
                  loading={latestLoading && latestValues.length === 0}
                />
              </Tooltip>
            </Space>
          </Col>
        </Row>
      </Card>

      {latestError && (
        <Alert
          type="error"
          showIcon
          message="加载失败"
          description={latestError}
          style={{ marginBottom: 16 }}
        />
      )}

      {/* Realtime panel */}
      <Card
        className="data-viz-page__panel"
        title={
          <Space size={12} wrap>
            <span>实时最新值</span>
            <Tag color="blue">匹配 {matchingCount} 个测点</Tag>
            {latestUpdatedAt && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                上次更新 {dayjs(latestUpdatedAt).format('HH:mm:ss')}
              </Text>
            )}
          </Space>
        }
        bordered
      >
        {tasksLoading || (latestLoading && latestValues.length === 0) ? (
          <div className="data-viz-page__center">
            <Spin tip="加载中..." />
          </div>
        ) : tasks.length === 0 ? (
          <Empty
            description={
              <span>
                没有运行中的采集任务，请前往{' '}
                <a onClick={() => navigate('/acquisition')}>采集控制</a> 启动任务
              </span>
            }
          />
        ) : matchingCount === 0 ? (
          <Empty description="当前筛选条件下没有测点" />
        ) : matchingCount > VIRTUALIZE_THRESHOLD ? (
          <VirtualPointGrid
            points={latestValues}
            renderCard={(p) => (
              <PointValueCard
                point={p}
                active={p.point_code === filter.pointCode}
                onSelect={onPointChange}
              />
            )}
          />
        ) : (
          <Row gutter={[12, 12]}>
            {latestValues.map((p) => (
              <Col key={`${p.device_id}-${p.point_code}`} xs={24} sm={12} md={8} xl={6}>
                <PointValueCard
                  point={p}
                  active={p.point_code === filter.pointCode}
                  onSelect={onPointChange}
                />
              </Col>
            ))}
          </Row>
        )}
      </Card>

      {/* History panel */}
      <Card
        className="data-viz-page__panel"
        title={
          selectedPoint ? (
            <Space direction="vertical" size={0}>
              <Space size={8} wrap>
                <Text strong>{selectedPoint.point_name || selectedPoint.point_code}</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {selectedPoint.point_code}
                  {selectedPoint.unit ? ` · ${selectedPoint.unit}` : ''}
                  {selectedPoint.device_name ? ` · ${selectedPoint.device_name}` : ''}
                </Text>
              </Space>
            </Space>
          ) : (
            '历史趋势'
          )
        }
        extra={
          selectedPoint ? (
            <Space>
              <Segmented<HistoryRange>
                options={HISTORY_OPTIONS}
                value={historyRange}
                onChange={(v) => {
                  setHistoryRange(v as HistoryRange);
                  setHistoryRefreshKey((n) => n + 1);
                }}
              />
              <Button
                icon={<DownloadOutlined />}
                onClick={handleExport}
                loading={exporting}
              >
                导出 CSV
              </Button>
            </Space>
          ) : null
        }
        bordered
      >
        {!selectedPoint ? (
          <Empty
            description="请选择具体测点查看历史"
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          >
            {ctaPoint && (
              <Button
                type="primary"
                icon={<ArrowRightOutlined />}
                onClick={() => onPointChange(ctaPoint.point_code)}
              >
                查看 {ctaPoint.point_name || ctaPoint.point_code} 历史
              </Button>
            )}
          </Empty>
        ) : (
          <Space direction="vertical" size={16} style={{ width: '100%' }}>
            <Row gutter={16}>
              <Col span={8}>
                <Statistic
                  title="当前值"
                  value={formatValue(selectedPoint.value)}
                  suffix={selectedPoint.unit || undefined}
                />
              </Col>
              <Col span={8}>
                <Statistic
                  title="数据类型"
                  value={selectedPoint.data_type || '—'}
                />
              </Col>
              <Col span={8}>
                <Statistic
                  title="最近时间"
                  value={
                    selectedPoint.timestamp
                      ? dayjs(selectedPoint.timestamp).format('YYYY-MM-DD HH:mm:ss')
                      : '—'
                  }
                />
              </Col>
            </Row>
            <PointChart
              pointCode={selectedPoint.point_code}
              startTime={historyTimeRange.start}
              endTime={historyTimeRange.end}
              unit={selectedPoint.unit}
              refreshKey={historyRefreshKey}
              height={340}
            />
          </Space>
        )}
      </Card>
    </div>
    </NowProvider>
  );
};

interface PointValueCardProps {
  point: PointLatestValue;
  active: boolean;
  onSelect: (pointCode: string) => void;
}

// React.memo: with a stable `onSelect` and no per-second `now` prop, an
// unchanged card skips re-rendering on poll refreshes and on the 1s clock tick
// (the ticking relative-time label re-renders in isolation via NowContext).
const PointValueCard: React.FC<PointValueCardProps> = React.memo(({ point, active, onSelect }) => {
  const quality = qualityToBadge(point.quality);
  const isUnavailable = point.value === null || point.value === undefined;

  return (
    <Card
      hoverable
      className={`point-value-card${active ? ' point-value-card--active' : ''}`}
      bordered
      onClick={() => onSelect(point.point_code)}
      bodyStyle={{ padding: 14 }}
    >
      <div className="point-value-card__header">
        <Tooltip title={point.point_code}>
          <span className="point-value-card__name">
            {point.point_name || point.point_code}
          </span>
        </Tooltip>
        <Badge status={quality.status} />
      </div>
      <div className="point-value-card__code">{point.point_code}</div>
      <div className="point-value-card__value-row">
        <span
          className={`point-value-card__value${isUnavailable ? ' point-value-card__value--empty' : ''}`}
        >
          {formatValue(point.value)}
        </span>
        {point.unit && <span className="point-value-card__unit">{point.unit}</span>}
      </div>
      <div className="point-value-card__footer">
        <Tooltip title={point.device_name}>
          <Text type="secondary" ellipsis style={{ fontSize: 12, maxWidth: 140 }}>
            {point.device_name}
          </Text>
        </Tooltip>
        <Text type="secondary" style={{ fontSize: 12 }}>
          <RelativeTime iso={point.timestamp} />
        </Text>
      </div>
    </Card>
  );
});

PointValueCard.displayName = 'PointValueCard';

export default DataVisualizationPage;
