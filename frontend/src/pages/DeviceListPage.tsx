import { useEffect, useState } from 'react';

interface Device {
  id: number;
  code: string;
  name: string;
  protocol: string;
  ip_address: string;
  port: number | null;
  site: number;
  metadata: Record<string, unknown>;
}

interface Point {
  id: number;
  code: string;
  description: string;
  device: number;
  to_kafka: boolean;
}

const DeviceListPage = () => {
  const [devices, setDevices] = useState<Device[]>([]);
  const [points, setPoints] = useState<Record<number, Point[]>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const loadDevices = async () => {
      setLoading(true);
      setError(null);
      try {
        const resp = await fetch('/api/config/devices/?distinct=1&site_code=default');
        if (!resp.ok) {
          throw new Error(await resp.text());
        }
        const data = (await resp.json()) as Device[];
        setDevices(data);

        const pointResp = await fetch('/api/config/points/');
        if (!pointResp.ok) {
          throw new Error(await pointResp.text());
        }
        const pointData = (await pointResp.json()) as Point[];
        const grouped: Record<number, Point[]> = {};
        pointData.forEach((p) => {
          grouped[p.device] = grouped[p.device] || [];
          grouped[p.device].push(p);
        });
        setPoints(grouped);
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setLoading(false);
      }
    };

    loadDevices();
  }, []);

  return (
    <section>
      <h2>连接与测点</h2>
      {loading && <p>加载中...</p>}
      {error && <p className="error">获取数据失败：{error}</p>}
      <div className="device-grid">
        {devices.map((device) => (
          <article key={device.id} className="card">
            <h3>{device.name || device.code}</h3>
            <p>
              <strong>协议：</strong> {device.protocol.toUpperCase()}
            </p>
            <p>
              <strong>地址：</strong> {device.ip_address}:{device.port ?? '-'}
            </p>
            <h4>测点列表</h4>
            <ul>
              {(points[device.id] || []).map((point) => (
                <li key={point.id}>
                  <span>{point.code}</span>
                  <span className={`status-tag ${point.to_kafka ? 'status-tag--success' : ''}`}>
                    {point.to_kafka ? 'Kafka' : '本地'}
                  </span>
                  <p className="small-text">{point.description}</p>
                </li>
              ))}
              {!(points[device.id]?.length) && <li>暂无测点</li>}
            </ul>
          </article>
        ))}
      </div>
    </section>
  );
};

export default DeviceListPage;
