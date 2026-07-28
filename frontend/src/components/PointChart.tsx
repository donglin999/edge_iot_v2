/**
 * Unified time-series line chart for a single point.
 * Replaces the old RealtimeChart / HistoricalTrendChart pair.
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
import { Empty, Spin } from 'antd';
import { fetchPointHistory } from '../services/dataApi';
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

interface PointChartProps {
  pointCode: string;
  startTime?: string;
  endTime?: string;
  unit?: string;
  height?: number;
  /** Bumping this value forces a re-fetch (e.g. after time-range change). */
  refreshKey?: number;
  /**
   * 服务端 last 降采样窗口(如 `10s` / `1m`)。传了则后端每窗口只回最新
   * 一条,减少传输与渲染压力;不传为全量数据(行为不变)。
   */
  window?: string;
}

interface ChartPoint {
  ts: number;
  label: string;
  value: number;
  quality: string;
}

const PointChart: React.FC<PointChartProps> = ({
  pointCode,
  startTime,
  endTime,
  unit,
  height = 320,
  refreshKey = 0,
  window: aggWindow,
}) => {
  const [data, setData] = useState<ChartPoint[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const aborter = new AbortController();
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await fetchPointHistory(
          pointCode,
          startTime,
          endTime,
          1000,
          aborter.signal,
          aggWindow,
        );
        const rows: ChartPoint[] = (res.data || [])
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

        if (!aborter.signal.aborted) {
          // Downsample so the SVG line chart stays responsive on large ranges.
          setData(downsample(rows, MAX_CHART_POINTS));
        }
      } catch (err) {
        if (!aborter.signal.aborted && !isAbortError(err)) {
          setError((err as Error).message);
        }
      } finally {
        if (!aborter.signal.aborted) setLoading(false);
      }
    };
    load();
    return () => {
      aborter.abort();
    };
  }, [pointCode, startTime, endTime, refreshKey, aggWindow]);

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
