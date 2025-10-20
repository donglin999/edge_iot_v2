import { useCallback, useEffect, useState } from 'react';

interface TaskRun {
  task: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  worker: string | null;
  log_reference: string | null;
}

interface OverviewPayload {
  total_tasks: number;
  active_tasks: number;
  status: Record<string, number>;
  recent_runs: TaskRun[];
  generated_at: string;
}

interface TaskItem {
  id: number;
  code: string;
  name: string;
  is_active: boolean;
}

const DashboardPage = () => {
  const [data, setData] = useState<OverviewPayload | null>(null);
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchOverview = useCallback(async () => {
    const response = await fetch('/api/config/tasks/overview/?site_code=default');
    if (!response.ok) {
      throw new Error(await response.text());
    }
    const payload = (await response.json()) as OverviewPayload;
    setData(payload);
  }, []);

  const fetchTasks = useCallback(async () => {
    const response = await fetch('/api/config/tasks/?site_code=default');
    if (!response.ok) {
      throw new Error(await response.text());
    }
    const payload = (await response.json()) as TaskItem[];
    setTasks(payload);
  }, []);

  useEffect(() => {
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        await Promise.all([fetchOverview(), fetchTasks()]);
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setLoading(false);
      }
    };

    load();
  }, [fetchOverview, fetchTasks]);


  return (
    <section>
      <h2>任务总览</h2>
      {loading && <p>加载中...</p>}
      {error && <p className="error">获取任务概览失败：{error}</p>}
      {data && (
        <div className="card-grid">
          <div className="card">
            <h3>任务总数</h3>
            <p className="metric">{data.total_tasks}</p>
          </div>
          <div className="card">
            <h3>启用任务</h3>
            <p className="metric">{data.active_tasks}</p>
          </div>
          <div className="card">
            <h3>状态分布</h3>
            <ul>
              {Object.entries(data.status).map(([status, count]) => (
                <li key={status}>
                  <span className="status-tag">{status}</span>
                  <span>{count}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}

      <h3>任务列表</h3>
      <p style={{ marginBottom: '1rem' }}>
        查看任务列表，前往 <a href="/acquisition" style={{ color: '#007bff', textDecoration: 'underline' }}>采集控制</a> 页面进行启动/停止操作。
      </p>
      <table className="table">
        <thead>
          <tr>
            <th>任务编码</th>
            <th>名称</th>
            <th>启用状态</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((task) => (
            <tr key={task.id}>
              <td>{task.code}</td>
              <td>{task.name}</td>
              <td>{task.is_active ? '启用' : '停用'}</td>
            </tr>
          ))}
          {!tasks.length && (
            <tr>
              <td colSpan={3}>暂无任务</td>
            </tr>
          )}
        </tbody>
      </table>

      <h3>最近任务运行</h3>
      <table className="table">
        <thead>
          <tr>
            <th>任务编码</th>
            <th>状态</th>
            <th>Worker</th>
            <th>开始时间</th>
            <th>结束时间</th>
            <th>日志</th>
          </tr>
        </thead>
        <tbody>
          {data?.recent_runs.map((run) => (
            <tr key={`${run.task}-${run.started_at}-${run.log_reference}`}>
              <td>{run.task}</td>
              <td><span className={`status-tag status-tag--${run.status}`}>{run.status}</span></td>
              <td>{run.worker ?? '-'}</td>
              <td>{run.started_at ? new Date(run.started_at).toLocaleString() : '-'}</td>
              <td>{run.finished_at ? new Date(run.finished_at).toLocaleString() : '-'}</td>
              <td>{run.log_reference ?? '-'}</td>
            </tr>
          ))}
          {!data?.recent_runs.length && (
            <tr>
              <td colSpan={6}>暂无运行记录</td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
};

export default DashboardPage;
