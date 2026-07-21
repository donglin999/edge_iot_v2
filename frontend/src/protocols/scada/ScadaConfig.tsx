/**
 * scada 协议的专属配置界面(注册在 `protocols/registry.tsx` 的 `scada` 下)。
 *
 * 解决的问题:以前每台 scada 设备都要重复一整套 MQTT 连接参数
 * (broker/端口/TLS/账号/密码/qos/product_key/话题模板),真正随设备变的只有
 * `device_name` 和测点 `code`。所以这里把流程拆成两段:
 *
 *   1. 网关服务配置(配一次)—— 共享的连接参数,选一个已有网关或就地新建/编辑。
 *   2. 设备与测点 —— 每台设备只填 device_name,每个测点只填 code + 中文名 + 类型。
 *
 * 保存统一走网关的 `provision` 端点(幂等:重复保存是更新,不会重复建设备)。
 *
 * create 模式保留了批量能力(加 N 台设备 /「复制本设备(含测点)」/
 * 「测点应用到全部」),这正是「N 台同型号机器、同样 4 个测点」场景下最省事的地方;
 * edit 模式只作用于当前这一台设备(外加它的网关配置),所以批量按钮不出现。
 */
import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Divider,
  Empty,
  Form,
  Input,
  InputNumber,
  Row,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { CopyOutlined, DeleteOutlined, EditOutlined, PlusOutlined } from '@ant-design/icons';

import ScadaGatewayFormModal from '../../components/ScadaGatewayFormModal';
import { apiClient } from '../../services/apiClient';
import { unwrapList } from '../../services/pagination';
import {
  SCADA_DATA_TYPES,
  extractProvisionErrors,
  listGateways,
  provision,
  type ProvisionPayload,
  type ProvisionResponse,
  type ScadaDataType,
  type ScadaGateway,
} from '../../services/scadaApi';
import type { ProtocolConfigHandle, ProtocolConfigProps } from '../registry';

const { Text } = Typography;

interface PointRow {
  key: string;
  /** 编辑态里已存在的测点 id;新加的行为 undefined。 */
  id?: number;
  code: string;
  description: string;
  data_type: ScadaDataType;
  unit: string;
}

interface DeviceRow {
  key: string;
  device_name: string;
  name: string;
  points: PointRow[];
}

interface MetaFormValues {
  site?: number;
  task_enabled: boolean;
  task_code?: string;
  task_name?: string;
  sample_rate_hz?: number;
  is_active: boolean;
}

interface PointRecord {
  id: number;
  code: string;
  description: string;
  extra?: Record<string, unknown>;
}

const DATA_TYPE_OPTIONS = SCADA_DATA_TYPES.map((t) => ({ label: t, value: t }));

/** 真实场景是「1 网关 → N 设备 → 每台 ~4 测点」,新设备直接给 4 行空测点。 */
const DEFAULT_POINTS_PER_DEVICE = 4;

const ScadaConfig = forwardRef<ProtocolConfigHandle, ProtocolConfigProps>(
  ({ mode, deviceId, device, onSaved }, ref) => {
    const isEdit = mode === 'edit';

    const [gateways, setGateways] = useState<ScadaGateway[]>([]);
    const [loadingGateways, setLoadingGateways] = useState(true);
    const [selectedId, setSelectedId] = useState<number | undefined>();
    const [gatewayModalOpen, setGatewayModalOpen] = useState(false);
    const [editingGateway, setEditingGateway] = useState<ScadaGateway | undefined>();

    const [devices, setDevices] = useState<DeviceRow[]>([]);
    /** 编辑态进场时该设备已有的测点 id,用于识别「被删掉的行」。 */
    const [originalPointIds, setOriginalPointIds] = useState<number[]>([]);
    const [result, setResult] = useState<ProvisionResponse | null>(null);
    const [errors, setErrors] = useState<string[]>([]);
    const [metaForm] = Form.useForm<MetaFormValues>();

    // 行 key 只用于 React 协调,不进 payload,简单自增即可。
    const seq = useRef(0);
    const nextKey = useCallback(() => {
      seq.current += 1;
      return `r${seq.current}`;
    }, []);

    const blankPoint = useCallback(
      (): PointRow => ({ key: nextKey(), code: '', description: '', data_type: 'float', unit: '' }),
      [nextKey],
    );

    const blankDevice = useCallback(
      (): DeviceRow => ({
        key: nextKey(),
        device_name: '',
        name: '',
        points: Array.from({ length: DEFAULT_POINTS_PER_DEVICE }, blankPoint),
      }),
      [nextKey, blankPoint],
    );

    /**
     * 编辑态要知道设备挂在哪个网关下。设备序列化里没有 gateway 字段,但
     * provision 生成的 code 是确定的 `scada-{网关编码}-{device_name}`,
     * 所以按前缀反查;实在匹配不上且只有一个网关时就用它。
     */
    const inferGateway = useCallback(
      (list: ScadaGateway[], target?: { code: string }): number | undefined => {
        if (!target) return list.length === 1 ? list[0].id : undefined;
        const matched = list
          .filter((g) => target.code.startsWith(`scada-${g.code}-`))
          // 网关编码互为前缀时取更长(更精确)的那个。
          .sort((a, b) => b.code.length - a.code.length)[0];
        if (matched) return matched.id;
        return list.length === 1 ? list[0].id : undefined;
      },
      [],
    );

    const refreshGateways = useCallback(
      async (preferId?: number) => {
        setLoadingGateways(true);
        try {
          const data = await listGateways();
          setGateways(data);
          setSelectedId((current) => {
            if (preferId && data.some((g) => g.id === preferId)) return preferId;
            if (current && data.some((g) => g.id === current)) return current;
            return inferGateway(data, device);
          });
          return data;
        } catch {
          return [] as ScadaGateway[];
        } finally {
          setLoadingGateways(false);
        }
      },
      [device, inferGateway],
    );

    useEffect(() => {
      refreshGateways();
      // 只在挂载时拉一次;后续由网关弹窗保存/新建触发刷新。
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // 编辑态:把这台设备和它的测点灌进网格。
    useEffect(() => {
      if (!isEdit || !deviceId || !device) return;
      let cancelled = false;
      apiClient
        .get(`/config/devices/${deviceId}/points/`)
        .then((res) => {
          if (cancelled) return;
          const points = unwrapList<PointRecord>(res.data);
          const rows: PointRow[] = points.map((p) => ({
            key: nextKey(),
            id: p.id,
            code: p.code,
            description: p.description ?? '',
            data_type: ((p.extra?.data_type as ScadaDataType) || 'float') as ScadaDataType,
            unit: (p.extra?.unit as string) || '',
          }));
          setOriginalPointIds(rows.map((r) => r.id as number));
          setDevices([
            {
              key: nextKey(),
              device_name: String(device.metadata?.scada_device_name ?? ''),
              name: device.name,
              points: rows.length > 0 ? rows : [blankPoint()],
            },
          ]);
        })
        .catch(() => undefined);
      return () => {
        cancelled = true;
      };
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [isEdit, deviceId, device]);

    // 新建态:选中网关后先给一台空设备起头,省掉「先点添加设备」这一步。
    useEffect(() => {
      if (!isEdit && selectedId && devices.length === 0) {
        setDevices([blankDevice()]);
      }
    }, [isEdit, selectedId, devices.length, blankDevice]);

    // 编辑态站点默认跟随设备当前站点。
    useEffect(() => {
      if (isEdit && device) {
        metaForm.setFieldsValue({ site: device.site });
      }
    }, [isEdit, device, metaForm]);

    const selected = useMemo(
      () => gateways.find((g) => g.id === selectedId),
      [gateways, selectedId],
    );

    const updateDevice = useCallback((key: string, patch: Partial<DeviceRow>) => {
      setDevices((rows) => rows.map((d) => (d.key === key ? { ...d, ...patch } : d)));
    }, []);

    const updatePoint = useCallback(
      (deviceKey: string, pointKey: string, patch: Partial<PointRow>) => {
        setDevices((rows) =>
          rows.map((d) =>
            d.key === deviceKey
              ? { ...d, points: d.points.map((p) => (p.key === pointKey ? { ...p, ...patch } : p)) }
              : d,
          ),
        );
      },
      [],
    );

    /** 把这台设备的测点表复制给其它所有设备 —— N 台同型号时最省事的一步。 */
    const applyPointsToAll = useCallback(
      (source: DeviceRow) => {
        setDevices((rows) =>
          rows.map((d) =>
            d.key === source.key
              ? d
              // 复制出来的是新测点,不能带 id。
              : { ...d, points: source.points.map((p) => ({ ...p, id: undefined, key: nextKey() })) },
          ),
        );
        message.success('已把该设备的测点复制到其它设备');
      },
      [nextKey],
    );

    /** 复制整台设备(含测点),device_name 留空等待填写。 */
    const duplicateDevice = useCallback(
      (source: DeviceRow) => {
        setDevices((rows) => {
          const index = rows.findIndex((d) => d.key === source.key);
          const copy: DeviceRow = {
            key: nextKey(),
            device_name: '',
            name: '',
            points: source.points.map((p) => ({ ...p, id: undefined, key: nextKey() })),
          };
          return [...rows.slice(0, index + 1), copy, ...rows.slice(index + 1)];
        });
      },
      [nextKey],
    );

    /** 校验并构造 provision payload;返回 null 表示校验未通过。 */
    const buildPayload = async (): Promise<ProvisionPayload | null> => {
      const problems: string[] = [];

      let meta: MetaFormValues;
      try {
        meta = await metaForm.validateFields();
      } catch {
        return null;
      }

      const cleaned = devices
        .map((d) => ({
          ...d,
          device_name: d.device_name.trim(),
          name: d.name.trim(),
          points: d.points
            .map((p) => ({
              ...p,
              code: p.code.trim(),
              description: p.description.trim(),
              unit: p.unit.trim(),
            }))
            // 空行是编辑期的占位,不算配置错误,直接丢掉。
            .filter((p) => p.code !== '' || p.description !== ''),
        }))
        .filter((d) => d.device_name !== '' || d.points.length > 0);

      if (cleaned.length === 0) {
        problems.push('至少需要一台设备');
      }

      const seenDevices = new Set<string>();
      cleaned.forEach((d, i) => {
        const label = d.device_name || `第 ${i + 1} 台设备`;
        if (!d.device_name) {
          problems.push(`第 ${i + 1} 台设备:设备名(device_name)不能为空`);
        } else if (seenDevices.has(d.device_name)) {
          problems.push(`设备名重复:${d.device_name}`);
        } else {
          seenDevices.add(d.device_name);
        }

        if (d.points.length === 0) {
          problems.push(`${label}:至少需要一个测点`);
        }
        const seenPoints = new Set<string>();
        d.points.forEach((p, j) => {
          if (!p.code) {
            problems.push(`${label} 第 ${j + 1} 个测点:测点编码不能为空`);
          } else if (seenPoints.has(p.code)) {
            problems.push(`${label}:测点编码重复 ${p.code}`);
          } else {
            seenPoints.add(p.code);
          }
        });
      });

      if (meta.task_enabled && !meta.task_code?.trim()) {
        problems.push('已勾选「同时创建采集任务」,请填写任务编码');
      }

      if (problems.length > 0) {
        setErrors(problems);
        setResult(null);
        return null;
      }

      const payload: ProvisionPayload = {
        devices: cleaned.map((d) => ({
          device_name: d.device_name,
          ...(d.name ? { name: d.name } : {}),
          points: d.points.map((p) => ({
            code: p.code,
            description: p.description,
            data_type: p.data_type,
            unit: p.unit,
          })),
        })),
      };
      if (meta.site !== undefined && meta.site !== null) {
        payload.site = meta.site;
      }
      if (meta.task_enabled && meta.task_code) {
        payload.task = {
          code: meta.task_code.trim(),
          name: (meta.task_name || meta.task_code).trim(),
          sample_rate_hz: meta.sample_rate_hz ?? 1,
          is_active: meta.is_active ?? true,
        };
      }
      return payload;
    };

    /**
     * 编辑态里从网格删掉的测点,provision 不会替我们删(它只做 upsert),
     * 所以这里显式删掉,否则「删了一行,保存后又回来了」。
     */
    const deleteRemovedPoints = async () => {
      if (!isEdit) return;
      const keptIds = new Set(
        devices.flatMap((d) => d.points.map((p) => p.id).filter((id): id is number => !!id)),
      );
      const removed = originalPointIds.filter((id) => !keptIds.has(id));
      for (const id of removed) {
        await apiClient.delete(`/config/points/${id}/`);
      }
      if (removed.length > 0) {
        setOriginalPointIds((ids) => ids.filter((id) => keptIds.has(id)));
      }
    };

    useImperativeHandle(ref, () => ({
      async submit() {
        if (!selected) {
          setErrors(['请先选择或新建一个网关']);
          return false;
        }
        const payload = await buildPayload();
        if (!payload) return false;

        setErrors([]);
        try {
          const response = await provision(selected.id, payload);
          await deleteRemovedPoints();
          setResult(response);
          message.success(
            `已下发:${response.created.devices} 台设备 / ${response.created.points} 个测点`,
          );
          onSaved?.();
          return true;
        } catch (error) {
          setResult(null);
          setErrors(extractProvisionErrors(error));
          return false;
        }
      },
    }));

    const pointColumns = (row: DeviceRow): ColumnsType<PointRow> => [
      {
        title: '测点编码',
        dataIndex: 'code',
        width: '30%',
        render: (_v, p) => (
          <Input
            value={p.code}
            placeholder="如:N270400150027"
            style={{ fontFamily: 'monospace' }}
            onChange={(e) => updatePoint(row.key, p.key, { code: e.target.value })}
          />
        ),
      },
      {
        title: '中文名',
        dataIndex: 'description',
        width: '32%',
        render: (_v, p) => (
          <Input
            value={p.description}
            placeholder="如:注射压力实际值"
            onChange={(e) => updatePoint(row.key, p.key, { description: e.target.value })}
          />
        ),
      },
      {
        title: '类型',
        dataIndex: 'data_type',
        width: 110,
        render: (_v, p) => (
          <Select
            value={p.data_type}
            style={{ width: '100%' }}
            options={DATA_TYPE_OPTIONS}
            onChange={(v: ScadaDataType) => updatePoint(row.key, p.key, { data_type: v })}
          />
        ),
      },
      {
        title: '单位',
        dataIndex: 'unit',
        width: 100,
        render: (_v, p) => (
          <Input
            value={p.unit}
            placeholder="如:MPa"
            onChange={(e) => updatePoint(row.key, p.key, { unit: e.target.value })}
          />
        ),
      },
      {
        title: '',
        width: 48,
        align: 'right',
        render: (_v, p) => (
          <Button
            icon={<DeleteOutlined />}
            size="small"
            type="text"
            danger
            disabled={row.points.length <= 1}
            onClick={() =>
              updateDevice(row.key, { points: row.points.filter((x) => x.key !== p.key) })
            }
          />
        ),
      },
    ];

    /** 话题预览:让用户直观看到 device_name / code 是怎么被拼进话题的。 */
    const topicPreview = (deviceName: string) =>
      (selected?.topic_template ?? '')
        .replace('{product_key}', selected?.product_key || '{product_key}')
        .replace('{device_name}', deviceName || '{device_name}');

    const totalPoints = devices.reduce(
      (sum, d) => sum + d.points.filter((p) => p.code.trim() !== '').length,
      0,
    );

    return (
      <>
        <Card
          size="small"
          type="inner"
          title="1 · 网关服务配置(配一次)"
          style={{ marginBottom: 12 }}
          extra={
            <Space size="small">
              <Button
                size="small"
                icon={<EditOutlined />}
                disabled={!selected}
                onClick={() => {
                  setEditingGateway(selected);
                  setGatewayModalOpen(true);
                }}
              >
                编辑网关
              </Button>
              <Button
                size="small"
                type="primary"
                icon={<PlusOutlined />}
                onClick={() => {
                  setEditingGateway(undefined);
                  setGatewayModalOpen(true);
                }}
              >
                新建网关
              </Button>
            </Space>
          }
        >
          <Select
            style={{ width: '100%' }}
            loading={loadingGateways}
            value={selectedId}
            placeholder="选择一个网关(MQTT 连接参数由它提供)"
            onChange={(v: number) => setSelectedId(v)}
            options={gateways.map((g) => ({
              label: `${g.name} · ${g.code} — ${g.source_ip}:${g.source_port}(${g.device_count} 台设备)`,
              value: g.id,
            }))}
            notFoundContent={<Empty description="还没有网关 — 点击「新建网关」" />}
          />
          {selected && (
            <Alert
              type="success"
              showIcon
              style={{ marginTop: 12 }}
              message="连接参数已由网关提供,下面只需要填设备名和测点。"
              description={
                <Text style={{ fontFamily: 'monospace', fontSize: 12 }}>
                  {selected.source_ip}:{selected.source_port} ·{' '}
                  {selected.mqtt_use_tls ? 'TLS' : '明文'} · QoS {selected.mqtt_qos} ·{' '}
                  {selected.topic_template}
                </Text>
              }
            />
          )}
        </Card>

        <Card
          size="small"
          type="inner"
          title={isEdit ? '2 · 设备与测点' : '2 · 设备与测点(批量)'}
          extra={
            selected ? (
              <Text type="secondary">
                {devices.length} 台设备 / {totalPoints} 个测点
              </Text>
            ) : null
          }
        >
          {!selected ? (
            <Empty description="请先在上方选择一个网关" />
          ) : (
            <>
              {devices.map((row, index) => (
                <Card
                  key={row.key}
                  size="small"
                  type="inner"
                  style={{ marginBottom: 12 }}
                  title={
                    <Space wrap>
                      <Tag color="blue">设备 {index + 1}</Tag>
                      <Tooltip
                        title={
                          isEdit
                            ? '设备名是该设备的身份(设备编码由它推导),编辑时不可更改'
                            : undefined
                        }
                      >
                        <Input
                          value={row.device_name}
                          disabled={isEdit}
                          placeholder="设备名 device_name,如:A0201010001150403"
                          style={{ width: 280, fontFamily: 'monospace' }}
                          onChange={(e) =>
                            updateDevice(row.key, { device_name: e.target.value })
                          }
                        />
                      </Tooltip>
                      <Input
                        value={row.name}
                        placeholder="友好名(选填),如:注塑机1"
                        style={{ width: 180 }}
                        onChange={(e) => updateDevice(row.key, { name: e.target.value })}
                      />
                    </Space>
                  }
                  extra={
                    isEdit ? null : (
                      <Space size="small">
                        <Tooltip title="复制本设备(含测点)">
                          <Button
                            icon={<CopyOutlined />}
                            size="small"
                            onClick={() => duplicateDevice(row)}
                          />
                        </Tooltip>
                        <Tooltip title="把本设备的测点复制到其它所有设备">
                          <Button
                            size="small"
                            disabled={devices.length < 2}
                            onClick={() => applyPointsToAll(row)}
                          >
                            测点应用到全部
                          </Button>
                        </Tooltip>
                        <Tooltip title="删除设备">
                          <Button
                            icon={<DeleteOutlined />}
                            size="small"
                            danger
                            disabled={devices.length <= 1}
                            onClick={() =>
                              setDevices((rows) => rows.filter((d) => d.key !== row.key))
                            }
                          />
                        </Tooltip>
                      </Space>
                    )
                  }
                >
                  <Text type="secondary" style={{ fontSize: 12, fontFamily: 'monospace' }}>
                    话题:{topicPreview(row.device_name)}
                  </Text>
                  <Table<PointRow>
                    rowKey="key"
                    size="small"
                    style={{ marginTop: 8 }}
                    columns={pointColumns(row)}
                    dataSource={row.points}
                    pagination={false}
                    footer={() => (
                      <Button
                        type="dashed"
                        size="small"
                        icon={<PlusOutlined />}
                        onClick={() =>
                          updateDevice(row.key, { points: [...row.points, blankPoint()] })
                        }
                      >
                        添加测点
                      </Button>
                    )}
                  />
                </Card>
              ))}

              {!isEdit && (
                <Button
                  type="dashed"
                  block
                  icon={<PlusOutlined />}
                  onClick={() => setDevices((rows) => [...rows, blankDevice()])}
                >
                  添加设备
                </Button>
              )}

              <Divider orientation="left" plain>
                3 · 站点与采集任务(选填)
              </Divider>

              <Form
                form={metaForm}
                // 这个界面被挂在 DeviceFormModal 的 <Form> 里,再渲染一个真实
                // <form> 元素会嵌套非法标签,所以只要表单逻辑不要 DOM 节点。
                component={false}
                layout="vertical"
                // 采集任务默认不建:「添加设备」的主线是把设备/测点配好,
                // 强制填任务编码会让每次保存都先撞一次校验失败。
                initialValues={{
                  site: 1,
                  task_enabled: false,
                  sample_rate_hz: 1,
                  is_active: true,
                }}
              >
                <Row gutter={16}>
                  <Col span={6}>
                    <Form.Item name="site" label="站点 ID" tooltip="留空则由后端决定归属站点">
                      <InputNumber min={1} style={{ width: '100%' }} />
                    </Form.Item>
                  </Col>
                  <Col span={8}>
                    <Form.Item
                      name="task_enabled"
                      label="同时创建/更新采集任务"
                      valuePropName="checked"
                      tooltip="勾选后,设备/测点落库的同时建好采集任务,不必再去采集控制页配置"
                    >
                      <Switch
                        checkedChildren="是"
                        unCheckedChildren="否"
                        // 打开时顺手把任务编码填成网关编码,少一次手输。
                        onChange={(checked) => {
                          if (checked && selected && !metaForm.getFieldValue('task_code')) {
                            metaForm.setFieldsValue({
                              task_code: `task-${selected.code}`,
                              task_name: selected.name,
                            });
                          }
                        }}
                      />
                    </Form.Item>
                  </Col>
                </Row>

                <Form.Item
                  noStyle
                  shouldUpdate={(prev, next) => prev.task_enabled !== next.task_enabled}
                >
                  {({ getFieldValue }) =>
                    getFieldValue('task_enabled') ? (
                      <Row gutter={16}>
                        <Col span={6}>
                          <Form.Item
                            name="task_code"
                            label="任务编码"
                            rules={[{ required: true, message: '请输入任务编码' }]}
                          >
                            <Input placeholder="如:task-zhongshan" />
                          </Form.Item>
                        </Col>
                        <Col span={8}>
                          <Form.Item name="task_name" label="任务名称">
                            <Input placeholder="如:中山小家电注塑采集" />
                          </Form.Item>
                        </Col>
                        <Col span={5}>
                          <Form.Item name="sample_rate_hz" label="采样频率(Hz)">
                            <InputNumber min={0.01} step={0.5} style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                        <Col span={5}>
                          <Form.Item name="is_active" label="启用任务" valuePropName="checked">
                            <Switch checkedChildren="启用" unCheckedChildren="停用" />
                          </Form.Item>
                        </Col>
                      </Row>
                    ) : null
                  }
                </Form.Item>
              </Form>

              {errors.length > 0 && (
                <Alert
                  type="error"
                  showIcon
                  closable
                  style={{ marginBottom: 12 }}
                  message="保存失败"
                  onClose={() => setErrors([])}
                  description={
                    <ul style={{ margin: 0, paddingLeft: 18 }}>
                      {errors.map((e) => (
                        <li key={e}>{e}</li>
                      ))}
                    </ul>
                  }
                />
              )}

              {result && (
                <Alert
                  type="success"
                  showIcon
                  closable
                  style={{ marginBottom: 12 }}
                  message={`下发成功 — 新建 ${result.created.devices} 台设备、${result.created.points} 个测点`}
                  onClose={() => setResult(null)}
                />
              )}

              <Text type="secondary">重复保存同一份配置是更新,不会重复创建设备。</Text>
            </>
          )}
        </Card>

        <ScadaGatewayFormModal
          open={gatewayModalOpen}
          gateway={editingGateway}
          onClose={() => setGatewayModalOpen(false)}
          onSaved={(saved) => refreshGateways(saved.id)}
        />
      </>
    );
  },
);

ScadaConfig.displayName = 'ScadaConfig';

export default ScadaConfig;
