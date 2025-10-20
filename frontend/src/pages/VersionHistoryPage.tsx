import { useEffect, useState } from 'react';
import { fetchTaskVersions, fetchAllTasks, rollbackToVersion, ConfigVersion } from '../services/versionApi';

const VersionHistoryPage = () => {
  const [tasks, setTasks] = useState<Array<{ id: number; code: string; name: string }>>([]);
  const [selectedTaskId, setSelectedTaskId] = useState<number | null>(null);
  const [versions, setVersions] = useState<ConfigVersion[]>([]);
  const [selectedVersion, setSelectedVersion] = useState<ConfigVersion | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  // Load tasks on mount
  useEffect(() => {
    const loadTasks = async () => {
      try {
        const taskList = await fetchAllTasks();
        setTasks(taskList);
        if (taskList.length > 0) {
          setSelectedTaskId(taskList[0].id);
        }
      } catch (err) {
        setError((err as Error).message);
      }
    };
    loadTasks();
  }, []);

  // Load versions when task is selected
  useEffect(() => {
    if (selectedTaskId === null) return;

    const loadVersions = async () => {
      setLoading(true);
      setError(null);
      try {
        const versionList = await fetchTaskVersions(selectedTaskId);
        setVersions(versionList);
        setSelectedVersion(null);
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setLoading(false);
      }
    };

    loadVersions();
  }, [selectedTaskId]);

  const handleRollback = async (versionId: number) => {
    if (!confirm('确定要回滚到此版本吗？这将创建一个新版本作为回滚记录。')) {
      return;
    }

    setLoading(true);
    setError(null);
    setSuccess(null);

    try {
      const result = await rollbackToVersion(versionId);
      setSuccess(result.detail);
      // Reload versions
      if (selectedTaskId) {
        const versionList = await fetchTaskVersions(selectedTaskId);
        setVersions(versionList);
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const handleViewDetails = (version: ConfigVersion) => {
    setSelectedVersion(selectedVersion?.id === version.id ? null : version);
  };

  return (
    <section>
      <h2>配置版本历史</h2>

      <div style={{ marginBottom: '1.5rem' }}>
        <label htmlFor="task-select" style={{ display: 'block', marginBottom: '0.5rem', fontWeight: 'bold' }}>
          选择任务：
        </label>
        <select
          id="task-select"
          value={selectedTaskId || ''}
          onChange={(e) => setSelectedTaskId(Number(e.target.value))}
          style={{ padding: '0.5rem', fontSize: '1rem', width: '100%', maxWidth: '400px' }}
        >
          {tasks.map((task) => (
            <option key={task.id} value={task.id}>
              {task.code} - {task.name}
            </option>
          ))}
        </select>
      </div>

      {error && <p className="error">错误：{error}</p>}
      {success && <p style={{ color: 'green', padding: '0.5rem', backgroundColor: '#e8f5e9', border: '1px solid #4caf50' }}>✓ {success}</p>}

      {loading && <p>加载中...</p>}

      {!loading && versions.length === 0 && (
        <p style={{ color: '#666' }}>暂无版本历史</p>
      )}

      {!loading && versions.length > 0 && (
        <div style={{ marginTop: '1rem' }}>
          <h3>版本时间线（共 {versions.length} 个版本）</h3>
          <div style={{ borderLeft: '3px solid #007bff', paddingLeft: '1rem', marginTop: '1rem' }}>
            {versions.map((version, index) => (
              <div
                key={version.id}
                style={{
                  marginBottom: '1.5rem',
                  padding: '1rem',
                  backgroundColor: index === 0 ? '#e3f2fd' : '#f5f5f5',
                  border: '1px solid #ddd',
                  borderRadius: '4px',
                  position: 'relative',
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <div>
                    <strong style={{ fontSize: '1.1rem' }}>版本 {version.version}</strong>
                    {index === 0 && (
                      <span style={{ marginLeft: '0.5rem', padding: '0.2rem 0.5rem', backgroundColor: '#4caf50', color: 'white', fontSize: '0.8rem', borderRadius: '3px' }}>
                        最新
                      </span>
                    )}
                    <p style={{ margin: '0.5rem 0', color: '#666' }}>
                      {new Date(version.created_at).toLocaleString('zh-CN')}
                    </p>
                    <p style={{ margin: '0.5rem 0' }}>
                      <strong>备注：</strong>{version.summary || '无'}
                    </p>
                    <p style={{ margin: '0.5rem 0', color: '#666' }}>
                      <strong>创建者：</strong>{version.created_by || '未知'}
                    </p>
                    <p style={{ margin: '0.5rem 0', color: '#666' }}>
                      <strong>测点数量：</strong>{version.payload?.points?.length || 0}
                    </p>
                  </div>
                  <div style={{ display: 'flex', gap: '0.5rem' }}>
                    <button
                      type="button"
                      onClick={() => handleViewDetails(version)}
                      style={{ padding: '0.5rem 1rem' }}
                    >
                      {selectedVersion?.id === version.id ? '收起详情' : '查看详情'}
                    </button>
                    {index !== 0 && (
                      <button
                        type="button"
                        onClick={() => handleRollback(version.id)}
                        disabled={loading}
                        style={{ padding: '0.5rem 1rem', backgroundColor: '#ff9800', color: 'white' }}
                      >
                        回滚到此版本
                      </button>
                    )}
                  </div>
                </div>

                {selectedVersion?.id === version.id && (
                  <div style={{ marginTop: '1rem', padding: '1rem', backgroundColor: 'white', border: '1px solid #ddd', borderRadius: '4px' }}>
                    <h4>配置详情</h4>
                    <p><strong>设备：</strong>{version.payload?.device}</p>
                    <h5 style={{ marginTop: '1rem' }}>测点列表（{version.payload?.points?.length || 0}）：</h5>
                    <table className="table" style={{ marginTop: '0.5rem' }}>
                      <thead>
                        <tr>
                          <th>编码</th>
                          <th>地址</th>
                          <th>描述</th>
                          <th>采样率(Hz)</th>
                        </tr>
                      </thead>
                      <tbody>
                        {version.payload?.points?.map((point, idx) => (
                          <tr key={idx}>
                            <td>{point.code}</td>
                            <td>{point.address}</td>
                            <td>{point.description}</td>
                            <td>{point.sample_rate_hz}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    <details style={{ marginTop: '1rem' }}>
                      <summary style={{ cursor: 'pointer', fontWeight: 'bold' }}>查看完整JSON配置</summary>
                      <pre style={{ marginTop: '0.5rem', padding: '1rem', backgroundColor: '#f5f5f5', overflow: 'auto' }}>
                        {JSON.stringify(version.payload, null, 2)}
                      </pre>
                    </details>
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
};

export default VersionHistoryPage;
