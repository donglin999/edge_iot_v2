/**
 * Import Excel → validate → preview diff → apply.
 *
 * Each step's response is mapped to a clear AntD section. Row-level
 * validation errors (the importer now returns row+column+message) are shown
 * as a sortable table so the user can fix the source spreadsheet quickly.
 */
import { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Dropdown,
  Empty,
  Radio,
  Result,
  Space,
  Statistic,
  Steps,
  Table,
  Tag,
  Typography,
  Upload,
  message,
} from 'antd';
import {
  CheckCircleOutlined,
  CloudDownloadOutlined,
  CloudUploadOutlined,
  DownOutlined,
  FileExcelOutlined,
  InfoCircleOutlined,
  PlayCircleOutlined,
  WarningOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import type { UploadProps } from 'antd';

import { apiClient } from '../services/apiClient';
import { downloadTemplate, listProtocols, type ProtocolDescriptor } from '../services/protocolApi';

const { Text, Title } = Typography;

interface RowError {
  row: number;
  column: string;
  message: string;
  protocol: string;
}

interface ImportSummary {
  rows_parsed?: number;
  connection_count?: number;
  device_tag_count?: number;
  created_points?: number;
  warnings?: string[];
  errors?: string[];
  row_errors?: RowError[];
  metadata?: Record<string, string>;
  apply_result?: {
    mode?: string;
    device_created?: number;
    device_updated?: number;
    point_created?: number;
    point_updated?: number;
  };
}

interface ImportJob {
  id: number;
  status: string;
  source_name: string;
  summary: ImportSummary;
  created_at: string;
  updated_at: string;
}

type Mode = 'merge' | 'replace' | 'append';

const ImportJobPage = () => {
  const [job, setJob] = useState<ImportJob | null>(null);
  const [step, setStep] = useState(0);
  const [mode, setMode] = useState<Mode>('merge');
  const [uploading, setUploading] = useState(false);
  const [applying, setApplying] = useState(false);
  const [protocols, setProtocols] = useState<ProtocolDescriptor[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickedProtocols, setPickedProtocols] = useState<string[]>([]);

  useEffect(() => {
    listProtocols()
      .then((p) => {
        setProtocols(p);
        setPickedProtocols(p.map((x) => x.name));
      })
      .catch(() => undefined);
  }, []);

  const downloadAll = async () => {
    await downloadTemplate();
    message.success('已开始下载,请检查浏览器下载栏');
  };

  const downloadPicked = async () => {
    if (pickedProtocols.length === 0) {
      message.warning('请至少勾选一个协议');
      return;
    }
    await downloadTemplate(pickedProtocols);
    message.success(`已下载 ${pickedProtocols.length} 个协议的模板`);
    setPickerOpen(false);
  };

  const handleUpload: UploadProps['customRequest'] = async ({ file, onSuccess, onError }) => {
    setUploading(true);
    try {
      const fd = new FormData();
      fd.append('file', file as Blob);
      fd.append('triggered_by', 'web_user');
      const res = await apiClient.post<ImportJob>('/config/import-jobs/', fd, {
        headers: { 'Content-Type': 'multipart/form-data' },
      });
      // Poll once for celery validation result
      await new Promise((r) => setTimeout(r, 800));
      const refreshed = await apiClient.get<ImportJob>(`/config/import-jobs/${res.data.id}/`);
      setJob(refreshed.data);
      setStep(1);
      onSuccess?.(refreshed.data, new XMLHttpRequest());
      if (refreshed.data.status === 'failed' || (refreshed.data.summary.row_errors?.length ?? 0) > 0) {
        message.warning('校验完成,但发现错误,请下方修正');
      } else {
        message.success('校验通过,可以应用');
      }
    } catch (e) {
      onError?.(e as Error);
    } finally {
      setUploading(false);
    }
  };

  const handleApply = async () => {
    if (!job) return;
    setApplying(true);
    try {
      await apiClient.post(`/config/import-jobs/${job.id}/apply/`, {
        site_code: 'default',
        mode,
        created_by: 'web_user',
      });
      const refreshed = await apiClient.get<ImportJob>(`/config/import-jobs/${job.id}/`);
      setJob(refreshed.data);
      setStep(2);
      message.success('配置已写入');
    } finally {
      setApplying(false);
    }
  };

  const handleReset = () => {
    setJob(null);
    setStep(0);
    setMode('merge');
  };

  const rowErrors = job?.summary?.row_errors ?? [];
  const hasErrors = rowErrors.length > 0 || (job?.summary?.errors?.length ?? 0) > 0;

  const errorColumns: ColumnsType<RowError> = [
    { title: '行号', dataIndex: 'row', width: 80, sorter: (a, b) => a.row - b.row },
    {
      title: '协议',
      dataIndex: 'protocol',
      width: 120,
      render: (p) => p ? <Tag>{p}</Tag> : <Text type="secondary">—</Text>,
    },
    { title: '列', dataIndex: 'column', width: 140 },
    { title: '错误信息', dataIndex: 'message' },
  ];

  const protocolPicker = (
    <Card size="small" style={{ width: 320, padding: 8 }}>
      <Text strong style={{ display: 'block', marginBottom: 8 }}>
        勾选要导出的协议
      </Text>
      <Checkbox.Group
        value={pickedProtocols}
        onChange={(v) => setPickedProtocols(v as string[])}
        style={{ display: 'flex', flexDirection: 'column', gap: 6 }}
      >
        {protocols.map((p) => (
          <Checkbox key={p.name} value={p.name}>
            {p.label} <Text type="secondary" style={{ fontSize: 12 }}>· {p.category}</Text>
          </Checkbox>
        ))}
      </Checkbox.Group>
      <div style={{ marginTop: 12, textAlign: 'right' }}>
        <Button size="small" type="primary" onClick={downloadPicked}>
          下载选中模板
        </Button>
      </div>
    </Card>
  );

  return (
    <div style={{ padding: 24 }}>
      <Card variant="borderless" style={{ marginBottom: 16 }}>
        <Space style={{ width: '100%', justifyContent: 'space-between', alignItems: 'flex-start' }} wrap>
          <div style={{ flex: 1, minWidth: 320 }}>
            <Title level={3} style={{ margin: 0 }}>
              批量导入 / 配置同步
            </Title>
            <Text type="secondary">
              通过 Excel 一次性配置多设备、多测点。每行选择 protocol_type,系统会按对应协议字段做行级校验。
            </Text>
          </div>
          <Space>
            <Dropdown
              open={pickerOpen}
              onOpenChange={setPickerOpen}
              trigger={['click']}
              dropdownRender={() => protocolPicker}
            >
              <Button icon={<CloudDownloadOutlined />}>
                按协议下载 <DownOutlined />
              </Button>
            </Dropdown>
            <Button type="primary" icon={<CloudDownloadOutlined />} onClick={downloadAll}>
              下载全协议模板
            </Button>
          </Space>
        </Space>
      </Card>

      <Card variant="borderless" style={{ marginBottom: 16 }}>
        <Space direction="vertical" style={{ width: '100%' }}>
          <Space wrap>
            <InfoCircleOutlined style={{ color: '#1f6feb' }} />
            <Text strong>模板说明</Text>
          </Space>
          <Text type="secondary">
            模板中每个支持的协议预置了一行示例数据,字段值取自各协议 <code>FieldSpec.example</code>。
            原样上传即可通过校验并写入配置库
            (示例 IP/端口指向不存在的设备,启动采集会持续报「无法连接」,但可作为参考填法)。
            生产配置请把示例 IP / 端口 / 测点地址替换为现场真实值。
          </Text>
          <Space size="small" wrap>
            <Text type="secondary">当前支持协议:</Text>
            {protocols.map((p) => (
              <Tag key={p.name} color="blue">
                {p.label}
              </Tag>
            ))}
          </Space>
        </Space>
      </Card>

      <Card variant="borderless" style={{ marginBottom: 16 }}>
        <Steps
          current={step}
          items={[
            { title: '上传 Excel', icon: <CloudUploadOutlined /> },
            { title: '校验', icon: <WarningOutlined /> },
            { title: '写入', icon: <CheckCircleOutlined /> },
          ]}
        />
      </Card>

      {step === 0 && (
        <Card variant="borderless">
          <Upload.Dragger
            accept=".xlsx,.xls"
            multiple={false}
            customRequest={handleUpload}
            disabled={uploading}
            showUploadList={false}
          >
            <p className="ant-upload-drag-icon">
              <FileExcelOutlined style={{ fontSize: 48, color: '#52c41a' }} />
            </p>
            <p className="ant-upload-text">点击或拖拽 Excel 到此处</p>
            <p className="ant-upload-hint">
              支持 .xlsx / .xls。如需模板,请点右上"下载模板"。
            </p>
          </Upload.Dragger>
        </Card>
      )}

      {step >= 1 && job && (
        <>
          <Card
            title="校验结果"
            variant="borderless"
            style={{ marginBottom: 16 }}
            extra={<Button onClick={handleReset}>重新上传</Button>}
          >
            <Descriptions column={4} bordered size="small" style={{ marginBottom: 16 }}>
              <Descriptions.Item label="文件名">{job.source_name}</Descriptions.Item>
              <Descriptions.Item label="状态">
                {hasErrors ? (
                  <Tag color="red">校验失败</Tag>
                ) : (
                  <Tag color="green">{job.status}</Tag>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="协议">
                {job.summary.metadata?.protocols ?? '—'}
              </Descriptions.Item>
              <Descriptions.Item label="解析行数">{job.summary.rows_parsed ?? 0}</Descriptions.Item>
              <Descriptions.Item label="设备数">{job.summary.connection_count ?? 0}</Descriptions.Item>
              <Descriptions.Item label="测点数">{job.summary.created_points ?? 0}</Descriptions.Item>
              <Descriptions.Item label="错误行" span={2}>
                {rowErrors.length}
              </Descriptions.Item>
            </Descriptions>

            {(job.summary.errors ?? []).map((err, i) => (
              <Alert key={i} type="error" message={err} showIcon style={{ marginBottom: 8 }} />
            ))}

            {rowErrors.length > 0 && (
              <Card size="small" title={`${rowErrors.length} 处行级错误`} style={{ marginTop: 12 }}>
                <Table<RowError>
                  rowKey={(r, i) => `${r.row}-${r.column}-${i}`}
                  columns={errorColumns}
                  dataSource={rowErrors}
                  size="small"
                  pagination={{ pageSize: 10 }}
                />
              </Card>
            )}

            {!hasErrors && (
              <>
                <Alert
                  type="success"
                  showIcon
                  message="校验通过"
                  description="可以选择导入模式并写入配置库。"
                  style={{ marginBottom: 12 }}
                />
                <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                  <div>
                    <Text strong>导入模式:</Text>
                    <Radio.Group
                      style={{ marginLeft: 16 }}
                      value={mode}
                      onChange={(e) => setMode(e.target.value)}
                    >
                      <Radio value="merge">合并(默认):更新已有,新增缺失</Radio>
                      <Radio value="append">追加:仅新增,不动已有</Radio>
                      <Radio value="replace">替换:先清空再导入</Radio>
                    </Radio.Group>
                  </div>
                  {mode === 'replace' && (
                    <Alert
                      type="warning"
                      showIcon
                      message="替换模式将删除站点下所有设备/测点/任务,请谨慎操作。"
                    />
                  )}
                  <Button
                    type="primary"
                    icon={<PlayCircleOutlined />}
                    loading={applying}
                    onClick={handleApply}
                  >
                    写入配置库
                  </Button>
                </Space>
              </>
            )}
          </Card>
        </>
      )}

      {step === 2 && job?.summary.apply_result && (
        <Card variant="borderless">
          <Result
            status="success"
            title="导入成功"
            subTitle={`模式: ${job.summary.apply_result.mode}`}
            extra={[
              <Button type="primary" key="reset" onClick={handleReset}>
                继续导入
              </Button>,
            ]}
          >
            <Space size="large">
              <Statistic
                title="设备(新建)"
                value={job.summary.apply_result.device_created ?? 0}
              />
              <Statistic
                title="设备(更新)"
                value={job.summary.apply_result.device_updated ?? 0}
              />
              <Statistic
                title="测点(新建)"
                value={job.summary.apply_result.point_created ?? 0}
              />
              <Statistic
                title="测点(更新)"
                value={job.summary.apply_result.point_updated ?? 0}
              />
            </Space>
          </Result>
        </Card>
      )}

      {step === 0 && !job && (
        <Card variant="borderless" style={{ marginTop: 16 }}>
          <Empty description="尚未上传任何 Excel 文件" />
        </Card>
      )}
    </div>
  );
};

export default ImportJobPage;
