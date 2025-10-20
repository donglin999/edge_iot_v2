import { useRef, useState } from 'react';

interface ImportResponse {
  id: number;
  status: string;
  summary: Record<string, unknown>;
  created_at: string;
}

interface ImportDiff {
  connections: {
    to_create: Record<string, unknown>[];
    to_remove: Record<string, unknown>[];
    existing: Record<string, unknown>[];
  };
  points: {
    to_create: Record<string, unknown>[];
    to_remove: Record<string, unknown>[];
    existing: Record<string, unknown>[];
  };
  site_code: string;
}

const ImportJobPage = () => {
  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<ImportResponse | null>(null);
  const [diff, setDiff] = useState<ImportDiff | null>(null);
  const [applyResult, setApplyResult] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [importMode, setImportMode] = useState<'replace' | 'merge' | 'append'>('merge');
  const dropRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const handleFileSelect = (files: FileList | null) => {
    if (files && files.length > 0) {
      const selected = files[0];
      setFile(selected);
      setResult(null);
      setApplyResult(null);
      setDiff(null);
    }
  };

  const handleDrop = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();
    dropRef.current?.classList.remove('dropzone--active');
    handleFileSelect(event.dataTransfer.files);
  };

  const handleDragOver = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    dropRef.current?.classList.add('dropzone--active');
  };

  const handleDragLeave = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    dropRef.current?.classList.remove('dropzone--active');
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!file) {
      setError('请先选择 Excel 文件');
      return;
    }
    setLoading(true);
    setError(null);
    setResult(null);
    setApplyResult(null);
    setDiff(null);
    try {
      const formData = new FormData();
      formData.append('file', file);
      const response = await fetch('/api/config/import-jobs/', {
        method: 'POST',
        body: formData,
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const data = (await response.json()) as ImportResponse;
      setResult(data);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const fetchDiff = async (jobId: number) => {
    const response = await fetch(`/api/config/import-jobs/${jobId}/diff/`);
    if (!response.ok) {
      throw new Error(await response.text());
    }
    const diffData = (await response.json()) as ImportDiff;
    setDiff(diffData);
  };

  const handleApply = async () => {
    if (!result) return;
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(`/api/config/import-jobs/${result.id}/apply/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: importMode }),
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      const data = (await response.json()) as { detail: string; result: Record<string, unknown> };
      setApplyResult(data.result);
      await fetchDiff(result.id);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <section>
      <h2>导入作业</h2>
      <form className="form" onSubmit={handleSubmit}>
        <div
          ref={dropRef}
          className="dropzone"
          onDrop={handleDrop}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onClick={() => inputRef.current?.click()}
        >
          {file ? (
            <>
              <p>已选择文件：{file.name}</p>
              <p className="small-text">点击或拖拽可重新选择</p>
            </>
          ) : (
            <>
              <p>拖拽 Excel 文件到此处，或点击选择上传</p>
              <p className="small-text">支持 .xlsx / .xls</p>
            </>
          )}
          <input
            ref={inputRef}
            type="file"
            accept=".xlsx,.xls"
            style={{ display: 'none' }}
            onChange={(event) => handleFileSelect(event.target.files)}
          />
        </div>
        <button type="submit" disabled={loading}>上传并校验</button>
      </form>

      {error && <p className="error">操作失败：{error}</p>}
      {result && (
        <div className="card">
          <h3>校验结果</h3>
          <pre>{JSON.stringify(result.summary, null, 2)}</pre>

          <div style={{ marginTop: '1rem', marginBottom: '1rem' }}>
            <label htmlFor="import-mode" style={{ display: 'block', marginBottom: '0.5rem', fontWeight: 'bold' }}>
              导入模式：
            </label>
            <select
              id="import-mode"
              value={importMode}
              onChange={(e) => setImportMode(e.target.value as 'replace' | 'merge' | 'append')}
              style={{ padding: '0.5rem', fontSize: '1rem', marginRight: '1rem' }}
            >
              <option value="merge">合并模式（默认）- 更新已有记录，创建新记录</option>
              <option value="replace">替换模式 - 删除站点所有数据后重新导入</option>
              <option value="append">追加模式 - 仅创建新记录，不修改已有记录</option>
            </select>
          </div>

          {importMode === 'replace' && (
            <p style={{ color: '#d32f2f', marginBottom: '1rem', padding: '0.5rem', backgroundColor: '#fff3e0', border: '1px solid #ff9800' }}>
              ⚠️ 警告：替换模式将删除站点下所有设备、测点和任务，请谨慎操作！
            </p>
          )}

          <button onClick={handleApply} disabled={loading}>写入配置库</button>
        </div>
      )}
      {applyResult && (
        <div className="card">
          <h3>写入结果</h3>
          <pre>{JSON.stringify(applyResult, null, 2)}</pre>
        </div>
      )}
      {diff && (
        <div className="card">
          <h3>差异概览（站点：{diff.site_code}）</h3>
          <p>连接新增：{diff.connections.to_create.length}，移除：{diff.connections.to_remove.length}</p>
          <p>测点新增：{diff.points.to_create.length}，移除：{diff.points.to_remove.length}</p>
          <details>
            <summary>查看详细差异</summary>
            <pre>{JSON.stringify(diff, null, 2)}</pre>
          </details>
        </div>
      )}
    </section>
  );
};

export default ImportJobPage;
