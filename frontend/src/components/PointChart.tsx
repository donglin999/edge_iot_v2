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

import {
  buildCategoricalSeries,
  buildChangeEvents,
  classifySeries,
  toFiniteNumber,
  type CategoricalSeries,
  type ChangeEvent,
  type RawRow,
  type SeriesKind,
} from './pointChartDisplay';
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
  /** 自适应完整形态:后端 count 后决定回全量还是极值包络(点数有上界)。 */
  full?: boolean;
  /** 响应元数据回调(副标题展示包络窗口/原始点数用)。 */
  onMeta?: (meta: { downsampled: boolean; windowUsed: string | null; rawCount: number | null }) => void;
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
  full,
  onMeta,
}) => {
  const [data, setData] = useState<ChartPoint[]>([]);
  // 字符串测点画不了折线:保留原始行,按数据形态选阶梯状态图/变化记录列表。
  const [seriesKind, setSeriesKind] = useState<SeriesKind>('numeric');
  const [categorical, setCategorical] = useState<CategoricalSeries | null>(null);
  const [changeEvents, setChangeEvents] = useState<ChangeEvent[]>([]);
  // onMeta 走 ref:父组件每次 render 传新函数,直接进依赖会让图表反复重取。
  const onMetaRef = React.useRef(onMeta);
  useEffect(() => { onMetaRef.current = onMeta; }, [onMeta]);
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
          full,
        );
        onMetaRef.current?.({
          downsampled: !!res.downsampled,
          windowUsed: res.window_used ?? null,
          rawCount: res.raw_count ?? null,
        });
        const rawRows: RawRow[] = (res.data || [])
          .map((dp) => ({
            ts: new Date(dp.timestamp).getTime(),
            label: dayjs(dp.timestamp).format('HH:mm:ss'),
            value: dp.value,
            quality: dp.quality,
          }))
          .sort((a, b) => a.ts - b.ts);

        const kind = classifySeries(rawRows);

        if (!aborter.signal.aborted) {
          setSeriesKind(kind);
          if (kind === 'categorical') {
            setCategorical(buildCategoricalSeries(rawRows));
            setChangeEvents([]);
            setData([]);
          } else if (kind === 'text') {
            setChangeEvents(buildChangeEvents(rawRows));
            setCategorical(null);
            setData([]);
          } else {
            const rows: ChartPoint[] = rawRows
              .map((r) => ({
                ts: r.ts,
                label: r.label,
                value: toFiniteNumber(r.value),
                quality: r.quality,
              }))
              .filter((row) => Number.isFinite(row.value));
            setCategorical(null);
            setChangeEvents([]);
            // Downsample so the SVG line chart stays responsive on large ranges.
            setData(downsample(rows, MAX_CHART_POINTS));
          }
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
  }, [pointCode, startTime, endTime, refreshKey, aggWindow, full]);

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

  // 枚举型字符串:阶梯状态图,Y 轴刻度即状态文本。
  if (seriesKind === 'categorical' && categorical && categorical.points.length > 0) {
    const levels = categorical.levels;
    return (
      <div>
        <div style={{ fontSize: 12, color: 'rgba(0,0,0,0.45)', marginBottom: 4 }}>
          文本型测点:按状态阶梯展示({levels.length} 种状态)
        </div>
        <ResponsiveContainer width="100%" height={height}>
          <LineChart data={categorical.points} margin={{ top: 10, right: 24, left: 8, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(0,0,0,0.06)" />
            <XAxis dataKey="label" tick={{ fontSize: 11 }} minTickGap={32} />
            <YAxis
              type="number"
              domain={[-0.5, levels.length - 0.5]}
              ticks={levels.map((_, i) => i)}
              tickFormatter={(i: number) => levels[i] ?? ''}
              tick={{ fontSize: 11 }}
              width={Math.min(160, Math.max(48, 14 * Math.max(...levels.map((l) => l.length))))}
            />
            <Tooltip
              formatter={(_v: number, _n, item) => [
                (item?.payload as { text?: string })?.text ?? '',
                '状态',
              ]}
              labelFormatter={(label) => `时间: ${label}`}
            />
            <Line
              type="stepAfter"
              dataKey="levelIndex"
              stroke="#1677ff"
              dot={false}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    );
  }

  // 自由文本:变化记录列表(连续相同值去重,新变化在前)。
  if (seriesKind === 'text' && changeEvents.length > 0) {
    return (
      <div style={{ height, overflowY: 'auto' }}>
        <div style={{ fontSize: 12, color: 'rgba(0,0,0,0.45)', marginBottom: 4 }}>
          文本型测点:显示值变化记录(连续相同值已合并,共 {changeEvents.length} 次变化)
        </div>
        {changeEvents.map((e) => (
          <div
            key={e.ts + e.value}
            style={{
              display: 'flex', gap: 12, padding: '4px 8px', fontSize: 13,
              borderBottom: '1px solid rgba(0,0,0,0.05)',
            }}
          >
            <span style={{ color: 'rgba(0,0,0,0.45)', fontVariantNumeric: 'tabular-nums' }}>
              {e.label}
            </span>
            <span style={{ wordBreak: 'break-all' }}>{e.value}</span>
          </div>
        ))}
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

  // Y 轴自适应量程:从 0 起画会把「大基数小变化」压成直线(现场实锤:开合模
  // 次数 5024→5052 在 0~6000 轴上完全不可见)。取数据 min/max 加 8% 边距;
  // 完全平坦时按数值量级给一个最小跨度,保证连平线也居中可读。
  const finiteValues = data.map((d) => d.value).filter((v) => Number.isFinite(v));
  let yDomain: [number, number] | undefined;
  if (finiteValues.length > 0) {
    const lo = Math.min(...finiteValues);
    const hi = Math.max(...finiteValues);
    const span = hi - lo;
    const pad = span > 0 ? span * 0.08 : Math.max(1, Math.abs(hi) * 0.01);
    yDomain = [lo - pad, hi + pad];
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 10, right: 24, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="rgba(0,0,0,0.06)" />
        <XAxis dataKey="label" tick={{ fontSize: 11 }} minTickGap={32} />
        <YAxis
          domain={yDomain ?? ['auto', 'auto']}
          tickFormatter={(v: number) =>
            Math.abs(v) >= 1000 ? v.toLocaleString('en-US', { maximumFractionDigits: 1 }) : `${+v.toFixed(2)}`
          }
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
