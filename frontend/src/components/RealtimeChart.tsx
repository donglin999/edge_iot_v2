/**
 * Real-time data chart component using Recharts
 */
import React, { useState, useEffect, useCallback } from 'react';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts';
import { useWebSocket, WebSocketMessage, WebSocketStatus } from '../hooks/useWebSocket';

interface DataPoint {
  timestamp: string;
  value: number;
  quality: string;
}

interface RealtimeChartProps {
  sessionId: number;
  title?: string;
  maxDataPoints?: number;
  height?: number;
}

const RealtimeChart: React.FC<RealtimeChartProps> = ({
  sessionId,
  title = '实时数据',
  maxDataPoints = 50,
  height = 300,
}) => {
  const [dataPoints, setDataPoints] = useState<DataPoint[]>([]);
  const [pointCode, setPointCode] = useState<string>('');

  // WebSocket connection
  const handleMessage = useCallback((message: WebSocketMessage) => {
    if (message.type === 'data_point') {
      const data = message.data as {
        session_id: number;
        point_code: string;
        timestamp: string;
        value: number;
        quality: string;
      };

      if (data.session_id === sessionId) {
        setPointCode(data.point_code);
        setDataPoints((prev) => {
          const newPoint = {
            timestamp: new Date(data.timestamp).toLocaleTimeString(),
            value: typeof data.value === 'number' ? data.value : parseFloat(String(data.value)),
            quality: data.quality,
          };

          // Keep only the last N data points
          const updated = [...prev, newPoint];
          if (updated.length > maxDataPoints) {
            return updated.slice(updated.length - maxDataPoints);
          }
          return updated;
        });
      }
    }
  }, [sessionId, maxDataPoints]);

  const { status } = useWebSocket({
    url: `ws://localhost:8000/ws/acquisition/sessions/${sessionId}/`,
    onMessage: handleMessage,
  });

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
          }}
        >
          <p style={{ margin: 0 }}>
            <strong>时间:</strong> {payload[0].payload.timestamp}
          </p>
          <p style={{ margin: 0, color: '#8884d8' }}>
            <strong>数值:</strong> {payload[0].value.toFixed(2)}
          </p>
          <p style={{ margin: 0 }}>
            <strong>质量:</strong> {payload[0].payload.quality}
          </p>
        </div>
      );
    }
    return null;
  };

  // Connection status indicator
  const getStatusColor = () => {
    switch (status) {
      case WebSocketStatus.CONNECTED:
        return '#4caf50';
      case WebSocketStatus.CONNECTING:
        return '#ff9800';
      case WebSocketStatus.ERROR:
        return '#f44336';
      default:
        return '#9e9e9e';
    }
  };

  return (
    <div className="card">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
        <h3>{title}</h3>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          <span style={{ fontSize: '0.875rem', color: '#666' }}>
            {pointCode || '等待数据...'}
          </span>
          <div
            style={{
              width: '12px',
              height: '12px',
              borderRadius: '50%',
              backgroundColor: getStatusColor(),
            }}
            title={`WebSocket: ${status}`}
          />
        </div>
      </div>

      {dataPoints.length === 0 ? (
        <div
          style={{
            height: `${height}px`,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: '#999',
          }}
        >
          等待实时数据...
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={height}>
          <LineChart data={dataPoints}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis
              dataKey="timestamp"
              tick={{ fontSize: 12 }}
              interval="preserveStartEnd"
            />
            <YAxis tick={{ fontSize: 12 }} />
            <Tooltip content={<CustomTooltip />} />
            <Legend />
            <Line
              type="monotone"
              dataKey="value"
              stroke="#8884d8"
              strokeWidth={2}
              dot={{ r: 3 }}
              activeDot={{ r: 5 }}
              name="数值"
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      )}

      <div style={{ marginTop: '1rem', fontSize: '0.875rem', color: '#666' }}>
        数据点数: {dataPoints.length} / {maxDataPoints}
      </div>
    </div>
  );
};

export default RealtimeChart;
