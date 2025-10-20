/**
 * Historical data trend chart component
 */
import React, { useState, useEffect } from 'react';
import {
  LineChart,
  Line,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  Brush,
} from 'recharts';
import { fetchPointHistory, DataPoint } from '../services/dataApi';

interface ChartDataPoint {
  timestamp: string;
  displayTime: string;
  value: number;
  quality: string;
}

interface HistoricalTrendChartProps {
  pointCode: string;
  startTime?: string;
  endTime?: string;
  title?: string;
  height?: number;
  chartType?: 'line' | 'area';
  showBrush?: boolean;
}

const HistoricalTrendChart: React.FC<HistoricalTrendChartProps> = ({
  pointCode,
  startTime,
  endTime,
  title,
  height = 400,
  chartType = 'line',
  showBrush = true,
}) => {
  const [data, setData] = useState<ChartDataPoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const loadData = async () => {
      if (!pointCode) {
        return;
      }

      setLoading(true);
      setError(null);

      try {
        const response = await fetchPointHistory(pointCode, startTime, endTime, 1000);

        // Transform data for chart
        const chartData: ChartDataPoint[] = response.data.map((dp: DataPoint) => {
          const date = new Date(dp.timestamp);
          return {
            timestamp: dp.timestamp,
            displayTime: date.toLocaleString('zh-CN', {
              month: '2-digit',
              day: '2-digit',
              hour: '2-digit',
              minute: '2-digit',
            }),
            value: typeof dp.value === 'number' ? dp.value : parseFloat(String(dp.value)),
            quality: dp.quality,
          };
        });

        setData(chartData);
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setLoading(false);
      }
    };

    loadData();
  }, [pointCode, startTime, endTime]);

  // Custom tooltip
  const CustomTooltip = ({ active, payload }: any) => {
    if (active && payload && payload.length) {
      return (
        <div
          style={{
            backgroundColor: 'white',
            border: '1px solid #ccc',
            padding: '10px',
            borderRadius: '4px',
            boxShadow: '0 2px 4px rgba(0,0,0,0.1)',
          }}
        >
          <p style={{ margin: 0, fontSize: '0.875rem' }}>
            <strong>时间:</strong> {payload[0].payload.displayTime}
          </p>
          <p style={{ margin: 0, color: '#8884d8', fontSize: '0.875rem' }}>
            <strong>数值:</strong> {payload[0].value.toFixed(2)}
          </p>
          <p style={{ margin: 0, fontSize: '0.875rem' }}>
            <strong>质量:</strong> {payload[0].payload.quality}
          </p>
        </div>
      );
    }
    return null;
  };

  // Calculate statistics
  const stats = React.useMemo(() => {
    if (data.length === 0) return null;

    const values = data.map((d) => d.value);
    const max = Math.max(...values);
    const min = Math.min(...values);
    const avg = values.reduce((sum, v) => sum + v, 0) / values.length;

    return { max, min, avg, count: data.length };
  }, [data]);

  if (loading) {
    return (
      <div className="card">
        <h3>{title || `测点趋势: ${pointCode}`}</h3>
        <div
          style={{
            height: `${height}px`,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: '#999',
          }}
        >
          加载中...
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="card">
        <h3>{title || `测点趋势: ${pointCode}`}</h3>
        <div
          style={{
            height: `${height}px`,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: '#f44336',
          }}
        >
          错误: {error}
        </div>
      </div>
    );
  }

  if (data.length === 0) {
    return (
      <div className="card">
        <h3>{title || `测点趋势: ${pointCode}`}</h3>
        <div
          style={{
            height: `${height}px`,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: '#999',
          }}
        >
          暂无数据
        </div>
      </div>
    );
  }

  return (
    <div className="card">
      <h3>{title || `测点趋势: ${pointCode}`}</h3>

      {/* Statistics */}
      {stats && (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(4, 1fr)',
            gap: '1rem',
            marginBottom: '1rem',
            padding: '1rem',
            backgroundColor: '#f5f5f5',
            borderRadius: '4px',
          }}
        >
          <div>
            <div style={{ fontSize: '0.75rem', color: '#666' }}>数据点数</div>
            <div style={{ fontSize: '1.25rem', fontWeight: 'bold' }}>{stats.count}</div>
          </div>
          <div>
            <div style={{ fontSize: '0.75rem', color: '#666' }}>最大值</div>
            <div style={{ fontSize: '1.25rem', fontWeight: 'bold', color: '#f44336' }}>
              {stats.max.toFixed(2)}
            </div>
          </div>
          <div>
            <div style={{ fontSize: '0.75rem', color: '#666' }}>最小值</div>
            <div style={{ fontSize: '1.25rem', fontWeight: 'bold', color: '#2196f3' }}>
              {stats.min.toFixed(2)}
            </div>
          </div>
          <div>
            <div style={{ fontSize: '0.75rem', color: '#666' }}>平均值</div>
            <div style={{ fontSize: '1.25rem', fontWeight: 'bold', color: '#4caf50' }}>
              {stats.avg.toFixed(2)}
            </div>
          </div>
        </div>
      )}

      {/* Chart */}
      <ResponsiveContainer width="100%" height={height}>
        {chartType === 'area' ? (
          <AreaChart data={data}>
            <defs>
              <linearGradient id="colorValue" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#8884d8" stopOpacity={0.8} />
                <stop offset="95%" stopColor="#8884d8" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis
              dataKey="displayTime"
              tick={{ fontSize: 11 }}
              interval="preserveStartEnd"
            />
            <YAxis tick={{ fontSize: 12 }} />
            <Tooltip content={<CustomTooltip />} />
            <Legend />
            {showBrush && <Brush dataKey="displayTime" height={30} stroke="#8884d8" />}
            <Area
              type="monotone"
              dataKey="value"
              stroke="#8884d8"
              fillOpacity={1}
              fill="url(#colorValue)"
              name="数值"
            />
          </AreaChart>
        ) : (
          <LineChart data={data}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis
              dataKey="displayTime"
              tick={{ fontSize: 11 }}
              interval="preserveStartEnd"
            />
            <YAxis tick={{ fontSize: 12 }} />
            <Tooltip content={<CustomTooltip />} />
            <Legend />
            {showBrush && <Brush dataKey="displayTime" height={30} stroke="#8884d8" />}
            <Line
              type="monotone"
              dataKey="value"
              stroke="#8884d8"
              strokeWidth={2}
              dot={false}
              name="数值"
            />
          </LineChart>
        )}
      </ResponsiveContainer>
    </div>
  );
};

export default HistoricalTrendChart;
