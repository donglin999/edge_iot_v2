/**
 * SCADA 网关配置页。
 *
 * 解决的问题:以前每台 scada 设备都要重复一整套 MQTT 连接参数
 * (broker/端口/TLS/账号/密码/qos/product_key/话题模板),真正随设备变的
 * 只有 `device_name` 和测点 `code`。
 *
 * 这里把流程拆成两步:
 *   1. 网关服务配置(配一次)—— 共享的连接参数。
 *   2. 设备与测点(批量)—— 每台设备只填 device_name,每个测点只填
 *      code + 中文名 + 类型,一次 provision 全部落库(顺带建采集任务)。
 *
 * provision 是幂等的:同一份配置重复保存是更新,不会重复建设备。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Divider,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
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
import {
  CopyOutlined,
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
  SaveOutlined,
} from '@ant-design/icons';

import ScadaGatewayFormModal from '../components/ScadaGatewayFormModal';
import {
  SCADA_DATA_TYPES,
  deleteGateway,
  extractProvisionErrors,
  listGateways,
  provision,
  type ProvisionPayload,
  type ProvisionResponse,
  type ScadaDataType,
  type ScadaGateway,
} from '../services/scadaApi';

const { Text, Title } = Typography;

interface PointRow {
  key: string;
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

const DATA_TYPE_OPTIONS = SCADA_DATA_TYPES.map((t) => ({ label: t, value: t }));

/** 真实场景是「1 网关 → N 设备 → 每台 ~4 测点」,新设备直接给 4 行空测点。 */
const DEFAULT_POINTS_PER_DEVICE = 4;

const ScadaGatewayPage = () => {
  const [gateways, setGateways] = useState<ScadaGateway[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<number | undefined>();
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<ScadaGateway | undefined>();

  const [devices, setDevices] = useState<DeviceRow[]>([]);
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<ProvisionResponse | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [metaForm] = Form.useForm<MetaFormValues>();

  // 行 key 只用于 React 协调,不进 payload,所以简单自增即可。
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

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listGateways();
      setGateways(data);
      // 只有一个网关时直接选中,省一次点击。
      setSelectedId((current) => {
        if (current && data.some((g) => g.id === current)) return current;
        return data.length === 1 ? data[0].id : undefined;
      });
    } catch {
      // 拦截器已提示
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const selected = useMemo(
    () => gateways.find((g) => g.id === selectedId),
    [gateways, selectedId],
  );

  // 选中网关后给一台空设备起头,省掉「先点添加设备」这一步。
  useEffect(() => {
    if (selectedId && devices.length === 0) {
      setDevices([blankDevice()]);
    }
  }, [selectedId, devices.length, blankDevice]);

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

  /** 把这台设备的测点表复制给其它所有设备——N 台机型号相同时最省事的一步。 */
  const applyPointsToAll = useCallback(
    (source: DeviceRow) => {
      setDevices((rows) =>
        rows.map((d) =>
          d.key === source.key
            ? d
            : { ...d, points: source.points.map((p) => ({ ...p, key: nextKey() })) },
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
          points: source.points.map((p) => ({ ...p, key: nextKey() })),
        };
        return [...rows.slice(0, index + 1), copy, ...rows.slice(index + 1)];
      });
    },
    [nextKey],
  );

  const handleDeleteGateway = (gateway: ScadaGateway) => {
    Modal.confirm({
      title: '确定删除该网关?',
      content:
        gateway.device_count > 0
          ? `${gateway.name} 下还挂着 ${gateway.device_count} 台设备,删除后这些设备将失去共享连接配置。`
          : `${gateway.name} (${gateway.code})`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        await deleteGateway(gateway.id);
        message.success('已删除');
        if (selectedId === gateway.id) setSelectedId(undefined);
        refresh();
      },
    });
  };

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

  const handleSave = async () => {
    if (!selected) return;
    const payload = await buildPayload();
    if (!payload) return;

    setSaving(true);
    setErrors([]);
    try {
      const response = await provision(selected.id, payload);
      setResult(response);
      message.success(
        `已下发:${response.created.devices} 台设备 / ${response.created.points} 个测点`,
      );
      // device_count 变了,刷新网关列表。
      refresh();
    } catch (error) {
      setResult(null);
      setErrors(extractProvisionErrors(error));
    } finally {
      setSaving(false);
    }
  };

  const gatewayColumns: ColumnsType<ScadaGateway> = [
    {
      title: '网关',
      dataIndex: 'name',
      render: (name: string, row) => (
        <>
          <strong>{name}</strong>
          <div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {row.code}
            </Text>
          </div>
        </>
      ),
    },
    {
      title: 'Broker',
      render: (_v, row) => (
        <Space size={4}>
          <Text style={{ fontFamily: 'monospace' }}>
            {row.source_ip}:{row.source_port}
          </Text>
          {row.mqtt_use_tls ? <Tag color="green">TLS</Tag> : <Tag>明文</Tag>}
          <Tag color="blue">QoS {row.mqtt_qos}</Tag>
        </Space>
      ),
    },
    {
      title: '产品 Key',
      dataIndex: 'product_key',
      render: (v: string) => <Text style={{ fontFamily: 'monospace' }}>{v || '—'}</Text>,
    },
    {
      title: '设备数',
      dataIndex: 'device_count',
      width: 90,
      render: (v: number) => <Badge count={v} showZero color={v > 0 ? '#1f6feb' : '#bbb'} />,
    },
    {
      title: '操作',
      width: 120,
      align: 'right',
      render: (_v, row) => (
        <Space size="small">
          <Tooltip title="编辑">
            <Button
              icon={<EditOutlined />}
              size="small"
              onClick={() => {
                setEditing(row);
                setModalOpen(true);
              }}
            />
          </Tooltip>
          <Tooltip title="删除">
            <Button
              icon={<DeleteOutlined />}
              size="small"
              danger
              onClick={() => handleDeleteGateway(row)}
            />
          </Tooltip>
        </Space>
      ),
    },
  ];

  const pointColumns = (device: DeviceRow): ColumnsType<PointRow> => [
    {
      title: '测点编码',
      dataIndex: 'code',
      width: '30%',
      render: (_v, row) => (
        <Input
          value={row.code}
          placeholder="如:N270400150027"
          style={{ fontFamily: 'monospace' }}
          onChange={(e) => updatePoint(device.key, row.key, { code: e.target.value })}
        />
      ),
    },
    {
      title: '中文名',
      dataIndex: 'description',
      width: '32%',
      render: (_v, row) => (
        <Input
          value={row.description}
          placeholder="如:注射压力实际值"
          onChange={(e) => updatePoint(device.key, row.key, { description: e.target.value })}
        />
      ),
    },
    {
      title: '类型',
      dataIndex: 'data_type',
      width: 110,
      render: (_v, row) => (
        <Select
          value={row.data_type}
          style={{ width: '100%' }}
          options={DATA_TYPE_OPTIONS}
          onChange={(v: ScadaDataType) => updatePoint(device.key, row.key, { data_type: v })}
        />
      ),
    },
    {
      title: '单位',
      dataIndex: 'unit',
      width: 100,
      render: (_v, row) => (
        <Input
          value={row.unit}
          placeholder="如:MPa"
          onChange={(e) => updatePoint(device.key, row.key, { unit: e.target.value })}
        />
      ),
    },
    {
      title: '',
      width: 48,
      align: 'right',
      render: (_v, row) => (
        <Button
          icon={<DeleteOutlined />}
          size="small"
          type="text"
          danger
          disabled={device.points.length <= 1}
          onClick={() =>
            updateDevice(device.key, { points: device.points.filter((p) => p.key !== row.key) })
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
    <div style={{ padding: 24 }}>
      <Card variant="borderless" style={{ marginBottom: 16 }}>
        <Space style={{ width: '100%', justifyContent: 'space-between', flexWrap: 'wrap' }}>
          <div>
            <Title level={3} style={{ margin: 0 }}>
              SCADA 网关
            </Title>
            <Text type="secondary">
              MQTT 连接参数配一次,之后加设备只填设备名、加测点只填编码
            </Text>
          </div>
          <Space wrap>
            <Button icon={<ReloadOutlined />} onClick={refresh}>
              刷新
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => {
                setEditing(undefined);
                setModalOpen(true);
              }}
            >
              新建网关
            </Button>
          </Space>
        </Space>
      </Card>

      <Card
        variant="borderless"
        title="1 · 网关服务配置(配一次)"
        style={{ marginBottom: 16 }}
        extra={<Text type="secondary">选中一个网关后,在下方批量添加设备</Text>}
      >
        <Table<ScadaGateway>
          rowKey="id"
          size="small"
          columns={gatewayColumns}
          dataSource={gateways}
          loading={loading}
          pagination={false}
          locale={{ emptyText: <Empty description="还没有网关 — 点击「新建网关」配置 MQTT 服务" /> }}
          rowSelection={{
            type: 'radio',
            selectedRowKeys: selectedId ? [selectedId] : [],
            onChange: (keys) => setSelectedId(keys[0] as number),
          }}
          onRow={(row) => ({ onClick: () => setSelectedId(row.id), style: { cursor: 'pointer' } })}
        />
      </Card>

      <Card
        variant="borderless"
        title="2 · 设备与测点(批量)"
        extra={
          selected ? (
            <Text type="secondary">
              当前网关:<strong>{selected.name}</strong> · {devices.length} 台设备 / {totalPoints}{' '}
              个测点
            </Text>
          ) : null
        }
      >
        {!selected ? (
          <Empty description="请先在上方选中一个网关" />
        ) : (
          <>
            <Alert
              type="success"
              showIcon
              style={{ marginBottom: 16 }}
              message="连接参数已由网关提供,这里只需要填设备名和测点。"
              description={
                <Text style={{ fontFamily: 'monospace', fontSize: 12 }}>
                  {selected.source_ip}:{selected.source_port} · {selected.mqtt_use_tls ? 'TLS' : '明文'}{' '}
                  · QoS {selected.mqtt_qos} · {selected.topic_template}
                </Text>
              }
            />

            {devices.map((device, index) => (
              <Card
                key={device.key}
                size="small"
                type="inner"
                style={{ marginBottom: 12 }}
                title={
                  <Space wrap>
                    <Tag color="blue">设备 {index + 1}</Tag>
                    <Input
                      value={device.device_name}
                      placeholder="设备名 device_name,如:A0201010001150403"
                      style={{ width: 280, fontFamily: 'monospace' }}
                      onChange={(e) => updateDevice(device.key, { device_name: e.target.value })}
                    />
                    <Input
                      value={device.name}
                      placeholder="友好名(选填),如:注塑机1"
                      style={{ width: 180 }}
                      onChange={(e) => updateDevice(device.key, { name: e.target.value })}
                    />
                  </Space>
                }
                extra={
                  <Space size="small">
                    <Tooltip title="复制本设备(含测点)">
                      <Button
                        icon={<CopyOutlined />}
                        size="small"
                        onClick={() => duplicateDevice(device)}
                      />
                    </Tooltip>
                    <Tooltip title="把本设备的测点复制到其它所有设备">
                      <Button
                        size="small"
                        disabled={devices.length < 2}
                        onClick={() => applyPointsToAll(device)}
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
                          setDevices((rows) => rows.filter((d) => d.key !== device.key))
                        }
                      />
                    </Tooltip>
                  </Space>
                }
              >
                <Text type="secondary" style={{ fontSize: 12, fontFamily: 'monospace' }}>
                  话题:{topicPreview(device.device_name)}
                </Text>
                <Table<PointRow>
                  rowKey="key"
                  size="small"
                  style={{ marginTop: 8 }}
                  columns={pointColumns(device)}
                  dataSource={device.points}
                  pagination={false}
                  footer={() => (
                    <Button
                      type="dashed"
                      size="small"
                      icon={<PlusOutlined />}
                      onClick={() =>
                        updateDevice(device.key, { points: [...device.points, blankPoint()] })
                      }
                    >
                      添加测点
                    </Button>
                  )}
                />
              </Card>
            ))}

            <Button
              type="dashed"
              block
              icon={<PlusOutlined />}
              onClick={() => setDevices((rows) => [...rows, blankDevice()])}
            >
              添加设备
            </Button>

            <Divider orientation="left" plain>
              3 · 站点与采集任务(选填)
            </Divider>

            <Form
              form={metaForm}
              layout="vertical"
              initialValues={{
                site: 1,
                task_enabled: true,
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
                <Col span={6}>
                  <Form.Item
                    name="task_enabled"
                    label="同时创建采集任务"
                    valuePropName="checked"
                    tooltip="勾选后,设备/测点落库的同时建好采集任务,不必再去采集控制页配置"
                  >
                    <Switch checkedChildren="是" unCheckedChildren="否" />
                  </Form.Item>
                </Col>
              </Row>

              <Form.Item noStyle shouldUpdate={(prev, next) => prev.task_enabled !== next.task_enabled}>
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
                style={{ marginBottom: 16 }}
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
                style={{ marginBottom: 16 }}
                message={`下发成功 — 新建/更新 ${result.created.devices} 台设备、${result.created.points} 个测点`}
                onClose={() => setResult(null)}
                description={
                  <>
                    {result.task && (
                      <div>
                        采集任务:<Text code>{result.task.code}</Text>(id {result.task.id})
                      </div>
                    )}
                    <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
                      {result.devices.map((d) => (
                        <li key={d.id}>
                          <Text code>{d.device_name}</Text> → {d.code} · {d.points.length} 个测点
                        </li>
                      ))}
                    </ul>
                  </>
                }
              />
            )}

            <Space style={{ marginTop: 8 }}>
              <Button
                type="primary"
                icon={<SaveOutlined />}
                loading={saving}
                onClick={handleSave}
              >
                保存并下发
              </Button>
              <Text type="secondary">
                重复保存同一份配置是更新,不会重复创建设备。
              </Text>
            </Space>
          </>
        )}
      </Card>

      <ScadaGatewayFormModal
        open={modalOpen}
        gateway={editing}
        onClose={() => setModalOpen(false)}
        onSaved={(saved) => {
          refresh();
          setSelectedId(saved.id);
        }}
      />
    </div>
  );
};

export default ScadaGatewayPage;
