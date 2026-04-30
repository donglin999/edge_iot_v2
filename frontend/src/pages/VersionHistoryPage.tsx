/**
 * 配置版本管理页。
 *
 * 展示所有任务的配置版本，支持：
 * - 顶栏：导出当前站点配置（按 site 选择）、按任务过滤、批量删除
 * - 行级：查看 payload 详情、下载该版本 Excel、回滚、删除
 * - 表格：分页 + 行选择
 */
import { useEffect, useMemo, useState } from 'react';
import {
  App,
  Button,
  Card,
  Descriptions,
  Drawer,
  Dropdown,
  Empty,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd';
import {
  CloudDownloadOutlined,
  DeleteOutlined,
  DownOutlined,
  DownloadOutlined,
  FileSearchOutlined,
  RollbackOutlined,
} from '@ant-design/icons';
import type { ColumnsType, TableRowSelection } from 'antd/es/table/interface';

import {
  bulkDeleteVersions,
  deleteVersion,
  exportCurrentConfig,
  exportVersion,
  fetchAllTasks,
  fetchSites,
  fetchTaskVersions,
  rollbackToVersion,
  type ConfigVersion,
  type Site,
} from '../services/versionApi';
import './VersionHistoryPage.css';

const { Text, Title } = Typography;

interface TaskOption {
  id: number;
  code: string;
  name: string;
}

const formatDateTime = (iso: string): string => {
  if (!iso) return '-';
  try {
    return new Date(iso).toLocaleString('zh-CN');
  } catch {
    return iso;
  }
};

const VersionHistoryPage = () => {
  const { message } = App.useApp();

  const [versions, setVersions] = useState<ConfigVersion[]>([]);
  const [tasks, setTasks] = useState<TaskOption[]>([]);
  const [sites, setSites] = useState<Site[]>([]);
  const [filterTaskId, setFilterTaskId] = useState<number | null>(null);
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [detailVersion, setDetailVersion] = useState<ConfigVersion | null>(null);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [siteDropdownOpen, setSiteDropdownOpen] = useState(false);

  const loadVersions = async (taskId: number | null) => {
    setLoading(true);
    try {
      const list = await fetchTaskVersions(taskId);
      setVersions(list);
      setSelectedRowKeys((prev) => prev.filter((k) => list.some((v) => v.id === k)));
    } catch (err) {
      message.error((err as Error).message || '加载版本列表失败');
    } finally {
      setLoading(false);
    }
  };

  // initial load: tasks, sites, all versions
  useEffect(() => {
    fetchAllTasks()
      .then(setTasks)
      .catch((err) => message.error((err as Error).message || '加载任务列表失败'));
    fetchSites()
      .then(setSites)
      .catch((err) => message.error((err as Error).message || '加载站点列表失败'));
    loadVersions(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleFilterChange = (value: number | null) => {
    setFilterTaskId(value);
    loadVersions(value);
  };

  const handleExportCurrent = async (siteCode: string) => {
    setExporting(true);
    try {
      await exportCurrentConfig(siteCode);
      message.success(`已导出站点 ${siteCode} 的当前配置`);
    } catch (err) {
      message.error((err as Error).message || '导出失败');
    } finally {
      setExporting(false);
      setSiteDropdownOpen(false);
    }
  };

  const handleExportClick = async () => {
    if (sites.length <= 1) {
      // 0 或 1 个 site 直接导出 default / 唯一站点
      const code = sites[0]?.code ?? 'default';
      await handleExportCurrent(code);
      return;
    }
    setSiteDropdownOpen(true);
  };

  const handleExportVersion = async (version: ConfigVersion) => {
    try {
      await exportVersion(version.id);
      message.success(`已下载版本 v${version.version}`);
    } catch (err) {
      message.error((err as Error).message || '下载失败');
    }
  };

  const handleRollback = async (version: ConfigVersion) => {
    try {
      const result = await rollbackToVersion(version.id);
      message.success(result.detail || '回滚成功');
      loadVersions(filterTaskId);
    } catch (err) {
      message.error((err as Error).message || '回滚失败');
    }
  };

  const handleDelete = async (version: ConfigVersion) => {
    try {
      await deleteVersion(version.id);
      message.success(`已删除版本 v${version.version}`);
      loadVersions(filterTaskId);
    } catch (err) {
      message.error((err as Error).message || '删除失败');
    }
  };

  const handleBulkDelete = async () => {
    if (selectedRowKeys.length === 0) return;
    try {
      const result = await bulkDeleteVersions(selectedRowKeys.map((k) => Number(k)));
      const deletedCount = result.deleted.length;
      const skippedCount = result.skipped.length;
      if (deletedCount > 0 && skippedCount === 0) {
        message.success(`已删除 ${deletedCount} 个版本`);
      } else if (deletedCount > 0 && skippedCount > 0) {
        const reasons = result.skipped.map((s) => `#${s.id}: ${s.reason}`).join('；');
        message.warning(`成功删除 ${deletedCount} 个，跳过 ${skippedCount} 个 (${reasons})`);
      } else if (skippedCount > 0) {
        const reasons = result.skipped.map((s) => `#${s.id}: ${s.reason}`).join('；');
        message.error(`未删除任何版本：${reasons}`);
      } else {
        message.info('未删除任何版本');
      }
      setSelectedRowKeys([]);
      loadVersions(filterTaskId);
    } catch (err) {
      message.error((err as Error).message || '批量删除失败');
    }
  };

  const taskOptions = useMemo(
    () => [
      { value: 'all', label: '全部任务' },
      ...tasks.map((t) => ({
        value: String(t.id),
        label: `${t.code} - ${t.name}`,
      })),
    ],
    [tasks],
  );

  // newest version per task is "latest"; we mark it so users see which one is current
  const latestPerTask = useMemo(() => {
    const map = new Map<number, number>();
    versions.forEach((v) => {
      const cur = map.get(v.task);
      if (cur === undefined || v.version > cur) map.set(v.task, v.version);
    });
    return map;
  }, [versions]);

  const columns: ColumnsType<ConfigVersion> = [
    {
      title: '版本',
      dataIndex: 'version',
      key: 'version',
      width: 120,
      render: (v: number, record) => (
        <Space size={6}>
          <Text strong>v{v}</Text>
          {latestPerTask.get(record.task) === v && <Tag color="blue">最新</Tag>}
        </Space>
      ),
      sorter: (a, b) => a.version - b.version,
    },
    {
      title: '任务',
      dataIndex: 'task_code',
      key: 'task_code',
      width: 220,
      render: (code: string | undefined, record) => {
        const fallback = tasks.find((t) => t.id === record.task);
        const display = code || fallback?.code || `#${record.task}`;
        return <Tag color="geekblue">{display}</Tag>;
      },
      filters: tasks.map((t) => ({ text: `${t.code} - ${t.name}`, value: t.id })),
      onFilter: (value, record) => record.task === value,
    },
    {
      title: '摘要',
      dataIndex: 'summary',
      key: 'summary',
      ellipsis: true,
      render: (s: string) => s || <Text type="secondary">-</Text>,
    },
    {
      title: '创建人',
      dataIndex: 'created_by',
      key: 'created_by',
      width: 140,
      render: (u: string) => u || <Text type="secondary">未知</Text>,
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 180,
      render: formatDateTime,
      sorter: (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
      defaultSortOrder: 'descend',
    },
    {
      title: '操作',
      key: 'actions',
      width: 280,
      fixed: 'right',
      render: (_, record) => {
        const isLatest = latestPerTask.get(record.task) === record.version;
        return (
          <Space size={4} wrap>
            <Button
              type="link"
              size="small"
              icon={<FileSearchOutlined />}
              onClick={() => setDetailVersion(record)}
            >
              详情
            </Button>
            <Button
              type="link"
              size="small"
              icon={<DownloadOutlined />}
              onClick={() => handleExportVersion(record)}
            >
              下载
            </Button>
            <Popconfirm
              title="回滚到此版本"
              description="将基于该版本创建一条新的版本记录。是否继续？"
              okText="回滚"
              cancelText="取消"
              onConfirm={() => handleRollback(record)}
              disabled={isLatest}
            >
              <Button
                type="link"
                size="small"
                icon={<RollbackOutlined />}
                disabled={isLatest}
              >
                回滚
              </Button>
            </Popconfirm>
            <Popconfirm
              title="删除此版本"
              description="删除后无法恢复。任务运行中将被后端阻止。"
              okText="删除"
              okType="danger"
              cancelText="取消"
              onConfirm={() => handleDelete(record)}
            >
              <Button type="link" size="small" danger icon={<DeleteOutlined />}>
                删除
              </Button>
            </Popconfirm>
          </Space>
        );
      },
    },
  ];

  const rowSelection: TableRowSelection<ConfigVersion> = {
    selectedRowKeys,
    onChange: setSelectedRowKeys,
  };

  const exportButton =
    sites.length > 1 ? (
      <Dropdown
        open={siteDropdownOpen}
        onOpenChange={setSiteDropdownOpen}
        trigger={['click']}
        menu={{
          items: sites.map((s) => ({
            key: s.code,
            label: `${s.code} - ${s.name}`,
            onClick: () => handleExportCurrent(s.code),
          })),
        }}
      >
        <Button type="primary" icon={<CloudDownloadOutlined />} loading={exporting}>
          <Space>
            导出当前配置
            <DownOutlined />
          </Space>
        </Button>
      </Dropdown>
    ) : (
      <Button
        type="primary"
        icon={<CloudDownloadOutlined />}
        loading={exporting}
        onClick={handleExportClick}
      >
        导出当前配置
      </Button>
    );

  return (
    <div className="version-history-page">
      <Card variant="borderless" className="version-history-page__header">
        <div className="version-history-page__header-row">
          <div>
            <Title level={3} style={{ margin: 0 }}>
              配置版本管理
            </Title>
            <Text type="secondary">
              查看、导出、回滚、删除站点下所有任务的配置版本快照。
            </Text>
          </div>
          <Space wrap>
            {exportButton}
            <Select
              style={{ minWidth: 220 }}
              value={filterTaskId === null ? 'all' : String(filterTaskId)}
              onChange={(v) => handleFilterChange(v === 'all' ? null : Number(v))}
              options={taskOptions}
              placeholder="任务过滤"
            />
            <Popconfirm
              title="批量删除版本"
              description={`将删除 ${selectedRowKeys.length} 个选中版本，运行中的任务版本会被后端跳过。`}
              okText="删除"
              okType="danger"
              cancelText="取消"
              onConfirm={handleBulkDelete}
              disabled={selectedRowKeys.length === 0}
            >
              <Button
                danger
                icon={<DeleteOutlined />}
                disabled={selectedRowKeys.length === 0}
              >
                批量删除{selectedRowKeys.length > 0 ? ` (${selectedRowKeys.length})` : ''}
              </Button>
            </Popconfirm>
          </Space>
        </div>
      </Card>

      <Card variant="borderless">
        <Table<ConfigVersion>
          rowKey="id"
          columns={columns}
          dataSource={versions}
          loading={loading}
          rowSelection={rowSelection}
          pagination={{
            defaultPageSize: 20,
            pageSizeOptions: [10, 20, 50, 100],
            showSizeChanger: true,
            showTotal: (total) => `共 ${total} 个版本`,
          }}
          scroll={{ x: 1100 }}
          locale={{
            emptyText: (
              <Empty description="暂无配置版本，请先到 /import 导入 Excel" />
            ),
          }}
        />
      </Card>

      <Drawer
        title={
          detailVersion
            ? `版本详情 - ${detailVersion.task_code ?? `#${detailVersion.task}`} v${detailVersion.version}`
            : '版本详情'
        }
        width={720}
        open={detailVersion !== null}
        onClose={() => setDetailVersion(null)}
        destroyOnClose
      >
        {detailVersion && (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Descriptions column={1} bordered size="small">
              <Descriptions.Item label="任务">
                {detailVersion.task_code ?? `#${detailVersion.task}`}
              </Descriptions.Item>
              <Descriptions.Item label="版本号">v{detailVersion.version}</Descriptions.Item>
              <Descriptions.Item label="创建人">
                {detailVersion.created_by || '未知'}
              </Descriptions.Item>
              <Descriptions.Item label="创建时间">
                {formatDateTime(detailVersion.created_at)}
              </Descriptions.Item>
              <Descriptions.Item label="摘要">
                {detailVersion.summary || '-'}
              </Descriptions.Item>
            </Descriptions>
            <div>
              <Text strong>Payload</Text>
              <pre className="version-history-page__payload">
                {JSON.stringify(detailVersion.payload, null, 2)}
              </pre>
            </div>
          </Space>
        )}
      </Drawer>
    </div>
  );
};

export default VersionHistoryPage;
