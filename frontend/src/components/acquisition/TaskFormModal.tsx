/**
 * 新建 / 编辑采集任务 —— 并进采集控制页的「任务管理」入口。
 *
 * 采集任务 = 一组测点 + 采样频率。以前只能从设备导入时自动生成,这个弹窗补上
 * 手动新建、改名、调频率、启停开关,以及**测点的增删改查**:
 *
 *   增:选设备 → 勾选它已有的测点纳入,或就地在设备上新建一个测点
 *   删:从任务移除(可选连带删除测点实体)
 *   改:改测点的编码 / 地址 / 中文名 / 类型
 *   查:按设备分组列出任务当前包含的测点
 *
 * 保存时:先把新建/改动的测点写库,再 PATCH/POST 任务把测点集合定下来。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  App,
  Button,
  Divider,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons';

import { fetchDevices, type Device } from '../../services/deviceApi';
import {
  createPoint,
  createTask,
  deletePoint,
  fetchDevicePoints,
  fetchTask,
  updatePoint,
  updateTask,
  type PointWritePayload,
  type TaskPoint,
} from '../../services/taskApi';

const { Text } = Typography;

const DATA_TYPES = ['float', 'int', 'bool', 'string'];

/** 任务里的一行测点(可能是已有的,也可能是本次新建/改动的)。 */
interface PointRow {
  key: string;
  /** 已存在测点的 id;本次新建的行为 undefined。 */
  id?: number;
  device: number;
  deviceName: string;
  code: string;
  address: string;
  description: string;
  data_type: string;
  /** 相比进场时是否改过(决定要不要写库)。 */
  dirty: boolean;
}

interface Props {
  open: boolean;
  /** 传 id = 编辑;不传 = 新建。 */
  taskId?: number;
  onClose: () => void;
  onSaved: () => void;
}

const TaskFormModal: React.FC<Props> = ({ open, taskId, onClose, onSaved }) => {
  const isEdit = taskId !== undefined;
  // antd5:静态 message 拿不到 ConfigProvider 的自定义 theme,会在控制台刷
  // "Static function can not consume context" 警告 —— 改用 App.useApp()
  // 拿 context-aware 的实例(App.tsx 的 <AntdApp> 已经包了)。
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [devices, setDevices] = useState<Device[]>([]);
  const [rows, setRows] = useState<PointRow[]>([]);
  const [originalPointIds, setOriginalPointIds] = useState<number[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);

  // 「添加测点」子表单
  const [addDevice, setAddDevice] = useState<number | undefined>();
  const [devicePoints, setDevicePoints] = useState<TaskPoint[]>([]);

  const seq = useMemo(() => ({ n: 0 }), []);
  const nextKey = useCallback(() => `r${(seq.n += 1)}`, [seq]);

  const deviceName = useCallback(
    (id: number) => devices.find((d) => d.id === id)?.name ?? `设备#${id}`,
    [devices],
  );

  // 打开时载入设备列表 + 编辑态载入任务与测点
  useEffect(() => {
    if (!open) return;
    setErrors([]);
    setAddDevice(undefined);
    setDevicePoints([]);
    setLoading(true);

    (async () => {
      const devs = await fetchDevices().catch(() => []);
      setDevices(devs);

      if (isEdit && taskId !== undefined) {
        const task = await fetchTask(taskId).catch(() => null);
        if (task) {
          form.setFieldsValue({
            name: task.name,
            code: task.code,
            sample_rate_hz: task.sample_rate_hz,
            is_active: task.is_active,
          });
          setOriginalPointIds(task.points);
          // 把任务的测点按设备取回来铺进表格。
          const byDevice = new Map<number, TaskPoint[]>();
          await Promise.all(
            devs.map(async (d) => {
              const pts = await fetchDevicePoints(d.id).catch(() => []);
              byDevice.set(d.id, pts);
            }),
          );
          const loaded: PointRow[] = [];
          for (const [devId, pts] of byDevice) {
            for (const p of pts) {
              if (task.points.includes(p.id)) {
                loaded.push({
                  key: nextKey(),
                  id: p.id,
                  device: devId,
                  deviceName: devs.find((d) => d.id === devId)?.name ?? `设备#${devId}`,
                  code: p.code,
                  address: p.address ?? '',
                  description: p.description ?? '',
                  data_type: String((p.extra?.data_type as string) || 'float'),
                  dirty: false,
                });
              }
            }
          }
          setRows(loaded);
        }
      } else {
        form.setFieldsValue({ sample_rate_hz: 1, is_active: true });
        setRows([]);
        setOriginalPointIds([]);
      }
      setLoading(false);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, taskId]);

  // 选了「添加测点」的设备后,拉它已有的测点供勾选。
  useEffect(() => {
    if (addDevice === undefined) {
      setDevicePoints([]);
      return;
    }
    fetchDevicePoints(addDevice).then(setDevicePoints).catch(() => setDevicePoints([]));
  }, [addDevice]);

  const patchRow = (key: string, patch: Partial<PointRow>) =>
    setRows((rs) => rs.map((r) => (r.key === key ? { ...r, ...patch, dirty: true } : r)));

  const removeRow = (key: string) => setRows((rs) => rs.filter((r) => r.key !== key));

  /** 把该设备已有的、还没在任务里的测点全部纳入。 */
  const addExistingPoints = () => {
    if (addDevice === undefined) return;
    const present = new Set(rows.filter((r) => r.id).map((r) => r.id));
    const fresh = devicePoints.filter((p) => !present.has(p.id));
    if (fresh.length === 0) {
      message.info('该设备的测点都已在任务里了');
      return;
    }
    setRows((rs) => [
      ...rs,
      ...fresh.map((p) => ({
        key: nextKey(),
        id: p.id,
        device: addDevice,
        deviceName: deviceName(addDevice),
        code: p.code,
        address: p.address ?? '',
        description: p.description ?? '',
        data_type: String((p.extra?.data_type as string) || 'float'),
        dirty: false,
      })),
    ]);
    message.success(`已纳入 ${fresh.length} 个测点`);
  };

  /** 在选中的设备上新建一个空测点行(保存时才写库)。 */
  const addNewPoint = () => {
    if (addDevice === undefined) {
      message.warning('请先选择设备');
      return;
    }
    setRows((rs) => [
      ...rs,
      {
        key: nextKey(),
        device: addDevice,
        deviceName: deviceName(addDevice),
        code: '',
        address: '',
        description: '',
        data_type: 'float',
        dirty: true,
      },
    ]);
  };

  // 一个任务只能绑一台设备:已有测点后,把设备锁定成第一行的设备。
  const lockedDevice = rows.length > 0 ? rows[0].device : undefined;

  // 锁定后,把「添加测点」的目标设备同步成锁定设备,让纳入/新建都作用在它上面。
  useEffect(() => {
    if (lockedDevice !== undefined && addDevice !== lockedDevice) {
      setAddDevice(lockedDevice);
    }
  }, [lockedDevice, addDevice]);

  const validate = (): string[] => {
    const problems: string[] = [];
    if (rows.length === 0) problems.push('至少需要一个测点');
    // 单设备约束(与后端一致):所有测点必须属于同一台设备。
    if (new Set(rows.map((r) => r.device)).size > 1) {
      problems.push('一个任务只能绑定一台设备的测点;跨设备请拆成多个任务');
    }
    const seen = new Set<string>();
    rows.forEach((r, i) => {
      if (!r.code.trim()) problems.push(`第 ${i + 1} 行:测点编码不能为空`);
      const dup = `${r.device}:${r.code.trim()}`;
      if (r.code.trim() && seen.has(dup)) {
        problems.push(`同一设备下测点编码重复:${r.code}`);
      }
      seen.add(dup);
    });
    return problems;
  };

  const handleSave = async () => {
    let meta: { name: string; code?: string; sample_rate_hz: number; is_active: boolean };
    try {
      meta = await form.validateFields();
    } catch {
      return;
    }
    const problems = validate();
    if (problems.length) {
      setErrors(problems);
      return;
    }
    setErrors([]);
    setSaving(true);
    try {
      // 1) 落库测点:新建的 POST,改过的 PATCH,拿到最终 id 集合。
      const pointIds: number[] = [];
      for (const r of rows) {
        const payload: PointWritePayload = {
          device: r.device,
          code: r.code.trim(),
          address: r.address.trim(),
          description: r.description.trim(),
          extra: { data_type: r.data_type },
        };
        if (r.id === undefined) {
          const created = await createPoint(payload);
          pointIds.push(created.id);
        } else {
          if (r.dirty) await updatePoint(r.id, payload);
          pointIds.push(r.id);
        }
      }

      // 2) 编辑态:进场时在任务里、现在被移除的测点,显式删掉测点实体。
      if (isEdit) {
        const kept = new Set(pointIds);
        const removed = originalPointIds.filter((id) => !kept.has(id));
        for (const id of removed) {
          await deletePoint(id).catch(() => undefined);
        }
      }

      // 3) 写任务本身。
      const taskPayload = {
        name: meta.name,
        sample_rate_hz: meta.sample_rate_hz,
        is_active: meta.is_active,
        points: pointIds,
      };
      if (isEdit && taskId !== undefined) {
        await updateTask(taskId, taskPayload);
        message.success('任务已更新');
      } else {
        await createTask({ ...taskPayload, code: meta.code });
        message.success('任务已创建');
      }
      onSaved();
      onClose();
    } catch (err) {
      const data = (err as { response?: { data?: unknown } })?.response?.data;
      setErrors([typeof data === 'string' ? data : JSON.stringify(data ?? (err as Error).message)]);
    } finally {
      setSaving(false);
    }
  };

  const columns: ColumnsType<PointRow> = [
    {
      title: '设备',
      dataIndex: 'deviceName',
      width: 140,
      render: (_v, r) => <Tag>{r.deviceName}</Tag>,
    },
    {
      title: '测点编码',
      width: 180,
      render: (_v, r) => (
        <Input
          value={r.code}
          placeholder="如:temperature"
          style={{ fontFamily: 'monospace' }}
          onChange={(e) => patchRow(r.key, { code: e.target.value })}
        />
      ),
    },
    {
      title: '地址',
      width: 150,
      render: (_v, r) => (
        <Input
          value={r.address}
          placeholder="推送型可留空"
          onChange={(e) => patchRow(r.key, { address: e.target.value })}
        />
      ),
    },
    {
      title: '中文名',
      render: (_v, r) => (
        <Input
          value={r.description}
          placeholder="如:温度"
          onChange={(e) => patchRow(r.key, { description: e.target.value })}
        />
      ),
    },
    {
      title: '类型',
      width: 100,
      render: (_v, r) => (
        <Select
          value={r.data_type}
          style={{ width: '100%' }}
          options={DATA_TYPES.map((t) => ({ label: t, value: t }))}
          onChange={(v) => patchRow(r.key, { data_type: v })}
        />
      ),
    },
    {
      title: '',
      width: 44,
      render: (_v, r) => (
        <Button type="text" danger icon={<DeleteOutlined />} onClick={() => removeRow(r.key)} />
      ),
    },
  ];

  return (
    <Modal
      open={open}
      title={isEdit ? '编辑任务' : '新建任务'}
      onCancel={onClose}
      onOk={handleSave}
      okText="保存"
      cancelText="取消"
      confirmLoading={saving}
      width={920}
      destroyOnHidden
    >
      <Form form={form} layout="vertical">
        <Space size="large" style={{ width: '100%' }} wrap>
          <Form.Item name="name" label="任务名称" rules={[{ required: true, message: '请输入任务名称' }]}>
            <Input placeholder="如:注塑车间采集" style={{ width: 220 }} />
          </Form.Item>
          {!isEdit && (
            <Form.Item
              name="code"
              label="任务编码"
              rules={[{ required: true, message: '请输入任务编码' }]}
              tooltip="唯一标识,创建后不可改"
            >
              <Input placeholder="如:task-injection" style={{ width: 200 }} />
            </Form.Item>
          )}
          <Form.Item name="sample_rate_hz" label="采样频率(Hz)" rules={[{ required: true }]}>
            <InputNumber min={0.1} max={100} step={0.1} style={{ width: 130 }} />
          </Form.Item>
          <Form.Item name="is_active" label="启用" valuePropName="checked">
            <Switch checkedChildren="是" unCheckedChildren="否" />
          </Form.Item>
        </Space>
      </Form>

      <Divider orientation="left" plain style={{ marginTop: 0 }}>
        测点 {rows.length > 0 && <Text type="secondary">（{rows.length} 个）</Text>}
      </Divider>

      {lockedDevice !== undefined && (
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
          ⓘ 一个任务只能绑一台设备（{deviceName(lockedDevice)}）。要采别的设备,请另建任务。
        </Text>
      )}

      {/* 添加测点工具条 */}
      <Space wrap style={{ marginBottom: 12 }}>
        <Select
          placeholder="选择设备"
          style={{ width: 220 }}
          // 已有测点后锁定成该设备:一个任务只能绑一台设备。
          value={lockedDevice ?? addDevice}
          disabled={lockedDevice !== undefined}
          showSearch
          optionFilterProp="label"
          onChange={setAddDevice}
          options={devices.map((d) => ({ label: `${d.name} · ${d.protocol}`, value: d.id }))}
        />
        <Button
          icon={<PlusOutlined />}
          disabled={addDevice === undefined}
          onClick={addExistingPoints}
        >
          纳入该设备已有测点
          {addDevice !== undefined && devicePoints.length > 0 ? `（${devicePoints.length}）` : ''}
        </Button>
        <Button icon={<PlusOutlined />} disabled={addDevice === undefined} onClick={addNewPoint}>
          新建测点
        </Button>
      </Space>

      {rows.length === 0 ? (
        <Empty description="还没有测点 —— 选一个设备,纳入已有测点或新建" />
      ) : (
        <Table<PointRow>
          rowKey="key"
          size="small"
          columns={columns}
          dataSource={rows}
          pagination={false}
          loading={loading}
          scroll={{ y: 300 }}
        />
      )}

      {/* 纯说明文字,不是需要确认的操作 —— 以前用 Popconfirm 包着,点一下弹出个
          没有 onConfirm 的确认框,是个无意义的空点击(rank22b)。改成静态 Alert。 */}
      {isEdit && originalPointIds.length > 0 && (
        <Alert
          type="info"
          showIcon
          style={{ marginTop: 8 }}
          message="移除测点 = 删除测点实体"
          description="保存时,从任务里移除的测点将被彻底删除,不只是从任务里解绑。"
        />
      )}

      {errors.length > 0 && (
        <Alert
          type="error"
          showIcon
          style={{ marginTop: 12 }}
          message="保存失败"
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {errors.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          }
        />
      )}
    </Modal>
  );
};

export default TaskFormModal;
