/**
 * 「测试连接」的过程弹窗。
 *
 * 以前点一下什么都不发生,几秒后蹦个 toast 说「连接失败」—— 既不知道系统在不在
 * 干活,也不知道是配置不全、TCP 不通、还是连上了但设备不响应。这三种情况的
 * 排障动作完全不同。
 *
 * 现在:点击**立刻**开弹窗,列出这次要走的四步,底下跑一个真实的秒表;后端返回
 * 后把每一步的真实结果与耗时填进去。
 *
 * 有一条自律:**在结果回来之前,除了「建立连接」之外的步骤一律显示「等待中」**。
 * 很容易顺手写成挨个假装亮起来,那是编的 —— 后端不流式推送,前端根本不知道
 * 它走到哪了。宁可少显示,不能显示假的。
 */
import { useEffect, useRef, useState } from 'react';
import { Alert, Modal, Space, Tag, Typography } from 'antd';
import {
  CheckCircleFilled,
  CloseCircleFilled,
  ExclamationCircleFilled,
  LoadingOutlined,
  MinusCircleOutlined,
} from '@ant-design/icons';

import { apiClient } from '../services/apiClient';

const { Text } = Typography;

export type StepStatus = 'ok' | 'failed' | 'skipped' | 'warning';

export interface TraceStep {
  key: string;
  label: string;
  status: StepStatus;
  detail: string;
  duration_ms: number;
}

export interface ConnectionTestResult {
  success: boolean;
  summary: string;
  message?: string;
  protocol?: string;
  steps: TraceStep[];
  total_ms?: number;
}

/** 后端固定按这四步走;结果没回来之前先用它把过程画出来。 */
const PLANNED_STEPS: Array<{ key: string; label: string }> = [
  { key: 'config', label: '检查设备配置' },
  { key: 'connect', label: '建立连接' },
  { key: 'handshake', label: '握手/健康检查' },
  { key: 'disconnect', label: '断开连接' },
];

const STEP_ICON: Record<StepStatus, JSX.Element> = {
  ok: <CheckCircleFilled style={{ color: '#52c41a' }} />,
  failed: <CloseCircleFilled style={{ color: '#ff4d4f' }} />,
  warning: <ExclamationCircleFilled style={{ color: '#faad14' }} />,
  skipped: <MinusCircleOutlined style={{ color: '#bfbfbf' }} />,
};

const STEP_TEXT: Record<StepStatus, string> = {
  ok: '通过',
  failed: '失败',
  warning: '异常',
  skipped: '已跳过',
};

interface Props {
  open: boolean;
  deviceId?: number;
  deviceName?: string;
  onClose: () => void;
}

const ConnectionTestModal: React.FC<Props> = ({ open, deviceId, deviceName, onClose }) => {
  const [result, setResult] = useState<ConnectionTestResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const running = open && result === null && error === null;
  const timer = useRef<number>();

  // 每次打开都重新发起一次,并把上次的结果清掉。
  useEffect(() => {
    if (!open || !deviceId) return;
    let cancelled = false;
    setResult(null);
    setError(null);
    setElapsed(0);

    apiClient
      .post(`/config/devices/${deviceId}/test-connection/`, undefined, {
        // 逐步结果由弹窗自己渲染,不需要全局 toast 再喊一遍。
        silent: true,
        // 后端最多阻塞 5s(超时会返回带步骤的 504),这里留一点余量。
        timeout: 20000,
      } as never)
      .then((res) => {
        if (!cancelled) setResult(res.data as ConnectionTestResult);
      })
      .catch((err) => {
        if (cancelled) return;
        // 504 也是有内容的响应 —— 后端在超时分支里照样给了步骤清单。
        const data = err?.response?.data as ConnectionTestResult | undefined;
        if (data?.steps) setResult(data);
        else setError(err?.message || '请求失败');
      });

    return () => {
      cancelled = true;
    };
  }, [open, deviceId]);

  // 真实秒表 —— 让人看得见「系统确实在干活」,而不是假装有进度。
  useEffect(() => {
    if (!running) {
      window.clearInterval(timer.current);
      return;
    }
    timer.current = window.setInterval(() => setElapsed((v) => v + 0.1), 100);
    return () => window.clearInterval(timer.current);
  }, [running]);

  const byKey = new Map((result?.steps ?? []).map((s) => [s.key, s]));

  return (
    <Modal
      open={open}
      title={deviceName ? `连接测试 · ${deviceName}` : '连接测试'}
      onCancel={onClose}
      onOk={onClose}
      okText="关闭"
      cancelButtonProps={{ style: { display: 'none' } }}
      width={560}
      destroyOnHidden
    >
      <div style={{ padding: '4px 0' }}>
        {PLANNED_STEPS.map((planned, index) => {
          const step = byKey.get(planned.key);
          // 结果没回来时:只有「建立连接」显示进行中 —— 前面的配置检查是本地
          // 校验、瞬间就过,后面的步骤我们确实不知道到没到,不能假装。
          const pending = !step;
          const active = pending && running && index <= 1;

          return (
            <div
              key={planned.key}
              style={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: 10,
                padding: '10px 0',
                borderBottom: index < PLANNED_STEPS.length - 1 ? '1px solid #f0f0f0' : 'none',
                opacity: pending && !active ? 0.45 : 1,
              }}
            >
              <span style={{ fontSize: 16, lineHeight: '22px', width: 18 }}>
                {step ? STEP_ICON[step.status] : active ? <LoadingOutlined spin /> : STEP_ICON.skipped}
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <Space size={8} wrap>
                  <Text strong>{planned.label}</Text>
                  {step && <Tag bordered={false}>{STEP_TEXT[step.status]}</Tag>}
                  {step && step.duration_ms > 0 && (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {step.duration_ms.toFixed(0)} ms
                    </Text>
                  )}
                  {!step && (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {active ? '进行中…' : '等待中'}
                    </Text>
                  )}
                </Space>
                {step?.detail && (
                  <div>
                    <Text type="secondary" style={{ fontSize: 12, wordBreak: 'break-all' }}>
                      {step.detail}
                    </Text>
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {running && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          已用时 {elapsed.toFixed(1)}s（超过 5s 无响应即判定不可达）
        </Text>
      )}

      {result && (
        <Alert
          style={{ marginTop: 12 }}
          type={result.success ? 'success' : 'error'}
          showIcon
          message={result.summary || result.message}
          description={
            result.total_ms !== undefined
              ? `协议 ${result.protocol ?? '—'} · 总耗时 ${result.total_ms.toFixed(0)} ms`
              : undefined
          }
        />
      )}

      {error && (
        <Alert style={{ marginTop: 12 }} type="error" showIcon message="请求失败" description={error} />
      )}
    </Modal>
  );
};

export default ConnectionTestModal;
