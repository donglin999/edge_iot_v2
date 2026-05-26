/**
 * Unified time-series line chart for a single point.
 *
 * When `taskId` is set the chart fetches via the M6 history proxy
 * (`fetchHistoryPoints`) so the request lands on the right edge in fleet
 * mode; with `taskId === null` it falls back to the legacy single-host
 * route (`fetchPointHistory`) so monolithic deployments keep working.
 *
 * Source / error metadata from the proxy is surfaced through
 * `onMetaChange`, which the page uses to render the "数据来源" tag and
 * partial-failure warnings above the chart.
 */
import React, { useEffect, useState } from 'react';
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import dayjs from 'dayjs';
import { Alert, Empty, Spin } from 'antd';
import {
  HistoryPointSample,
  HistoryPointsError,
  HistoryPointsResponse,
  fetchHistoryPoints,
  fetchPointHistory,
} from '../services/dataApi';
import { isAbortError } from '../services/http';

/**
 * Recharts renders every datum as SVG; thousands of points janks the chart.
 * Cap the series at this many points and downsample beyond it.
 */
const MAX_CHART_POINTS = 500;

/** Evenly downsample to at most `max` items, always keeping first & last. */
function downsample<T>(rows: T[], max: number): T[] {
  if (rows.length <= max) return rows;
  const step = (rows.length - 1) / (max - 1);
  const out: T[] = [];
  for (let i = 0; i < max; i += 1) {
    out.push(rows[Math.round(i * step)]);
  }
  return out;
}

export interface PointChartMeta {
  /**
   * First successful edge name from `res.sources` — what the UI displays
   * as "数据来源: <edge>". `null` for legacy single-host responses or
   * when every edge failed.
   */
  dataSource: string | null;
  /** Per-edge errors (offline, timeout, unreachable, …). */
  errors: Record<string, HistoryPointsError>;
  /** Number of edges that successfully returned at least one row. */
  successCount: number;
}

interface PointChartProps {
  pointCode: string;
  /**
   * `AcqTask.id` the selected point belongs to. When set the chart uses
   * the M6 proxy so the request goes to the owning edge. `null` keeps
   * the legacy single-host fetch (monolithic deployments / no task in
   * context).
   */
  taskId: number | null;
  startTime?: string;
  endTime?: string;
  unit?: string;
  height?: number;
  /** Bumping this value forces a re-fetch (e.g. after time-range change). */
  refreshKey?: number;
  /**
   * Notified after each fetch so the surrounding page can render the
   * "数据来源" tag and partial-edge-failure warnings.
   */
  onMetaChange?: (meta: PointChartMeta) => void;
}

interface ChartPoint {
  ts: number;
  label: string;
  value: number;
  quality: string;
}

const EMPTY_META: PointChartMeta = {
  dataSource: null,
  errors: {},
  successCount: 0,
};

function toChartPoints(samples: Array<HistoryPointSample | { timestamp: string; value: number | string | boolean | null; quality: string }>): ChartPoint[] {
  return samples
    .map((dp) => {
      const numericValue =
        typeof dp.value === 'number'
          ? dp.value
          : typeof dp.value === 'boolean'
          ? dp.value
            ? 1
            : 0
          : parseFloat(String(dp.value));
      return {
        ts: new Date(dp.timestamp).getTime(),
        label: dayjs(dp.timestamp).format('HH:mm:ss'),
        value: Number.isFinite(numericValue) ? numericValue : NaN,
        quality: dp.quality,
      };
    })
    .filter((row) => Number.isFinite(row.value))
    .sort((a, b) => a.ts - b.ts);
}

const PointChart: React.FC<PointChartProps> = ({
  pointCode,
  taskId,
  startTime,
  endTime,
  unit,
  height = 320,
  refreshKey = 0,
  onMetaChange,
}) => {
  const [data, setData] = useState<ChartPoint[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // When every targeted edge fails the proxy returns the per-edge error
  // payload but no rows. We replace the chart with a dedicated Alert in
  // that case instead of showing the generic "暂无数据" empty state.
  const [edgeErrors, setEdgeErrors] = useState<Record<string, HistoryPointsError>>({});

  useEffect(() => {
    const aborter = new AbortController();
    const reportMeta = (meta: PointChartMeta) => {
      if (!aborter.signal.aborted) onMetaChange?.(meta);
    };

    const load = async () => {
      setLoading(true);
      setError(null);
      setEdgeErrors({});
      try {
        if (taskId !== null) {
          const res: HistoryPointsResponse = await fetchHistoryPoints(
            {
              taskIds: [taskId],
              pointIds: [pointCode],
              start: startTime,
              end: endTime,
              limit: 1000,
            },
            aborter.signal,
          );
          const sourceNames = Object.keys(res.sources || {});
          const errs = res.errors || {};
          if (!aborter.signal.aborted) {
            setEdgeErrors(errs);
            setData(downsample(toChartPoints(res.data || []), MAX_CHART_POINTS));
          }
          reportMeta({
            dataSource: sourceNames[0] ?? null,
            errors: errs,
            successCount: sourceNames.length,
          });
        } else {
          const res = await fetchPointHistory(
            pointCode,
            startTime,
            endTime,
            1000,
            aborter.signal,
          );
          if (!aborter.signal.aborted) {
            setData(downsample(toChartPoints(res.data || []), MAX_CHART_POINTS));
          }
          reportMeta(EMPTY_META);
        }
      } catch (err) {
        if (!aborter.signal.aborted && !isAbortError(err)) {
          setError((err as Error).message);
          reportMeta(EMPTY_META);
        }
      } finally {
        if (!aborter.signal.aborted) setLoading(false);
      }
    };
    load();
    return () => {
      aborter.abort();
    };
    // `onMetaChange` is intentionally excluded — callers typically pass
    // an inline lambda, and including it would re-trigger the fetch on
    // every page render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pointCode, taskId, startTime, endTime, refreshKey]);

  if (loading && data.length === 0) {
    return (
      <div
        style={{
          height,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        <Spin tip="加载中..." />
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ height, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Empty description={`加载失败: ${error}`} />
      </div>
    );
  }

  // All targeted edges failed — show an explicit offline alert rather
  // than an empty chart so the operator knows why nothing is plotted.
  const errorEntries = Object.entries(edgeErrors);
  if (data.length === 0 && errorEntries.length > 0) {
    return (
      <div style={{ height, padding: 16 }}>
        <Alert
          type="error"
          showIcon
          message="所有数据源均不可用"
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {errorEntries.map(([edgeName, err]) => (
                <li key={edgeName}>
                  edge {edgeName} {err.code === 'edge_offline' ? '离线' : '不可达'}
                  {err.message ? `: ${err.message}` : ''}
                </li>
              ))}
            </ul>
          }
        />
      </div>
    );
  }

  if (data.length === 0) {
    return (
      <div style={{ height, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Empty description="暂无数据" />
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 10, right: 24, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="rgba(0,0,0,0.06)" />
        <XAxis dataKey="label" tick={{ fontSize: 11 }} minTickGap={32} />
        <YAxis
          tick={{ fontSize: 11 }}
          label={
            unit
              ? { value: unit, angle: -90, position: 'insideLeft', style: { fontSize: 11 } }
              : undefined
          }
        />
        <Tooltip
          formatter={(value: number) => [
            `${typeof value === 'number' ? value.toFixed(3) : value}${unit ? ` ${unit}` : ''}`,
            '值',
          ]}
          labelFormatter={(label) => `时间: ${label}`}
        />
        <Line
          type="monotone"
          dataKey="value"
          stroke="#1f7a8c"
          strokeWidth={2}
          dot={false}
          activeDot={{ r: 4 }}
          isAnimationActive={false}
          name={pointCode}
        />
      </LineChart>
    </ResponsiveContainer>
  );
};

export default PointChart;
