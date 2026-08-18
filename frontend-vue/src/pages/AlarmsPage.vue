<script setup lang="ts">
import {
  CheckOutlined,
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons-vue';
import {
  Alert as AAlert,
  App as AntApp,
  Badge as ABadge,
  Button as AButton,
  Card as ACard,
  Col as ACol,
  Form as AForm,
  FormItem as AFormItem,
  Input as AInput,
  InputNumber as AInputNumber,
  Modal as AModal,
  Row as ARow,
  Segmented as ASegmented,
  Select as ASelect,
  Space as ASpace,
  Switch as ASwitch,
  Table as ATable,
  TabPane as ATabPane,
  Tabs as ATabs,
  Tag as ATag,
  TypographyText as ATypographyText,
  TypographyTitle as ATypographyTitle,
  type FormInstance,
  type TableColumnsType,
} from 'ant-design-vue';
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';

import {
  acknowledgeAlarm,
  alarmRuleRangeError,
  createAlarmRule,
  deleteAlarmRule,
  fetchAlarmRules,
  fetchAlarms,
  isRangeAlarmOperator,
  updateAlarmRule,
  type AlarmRecord,
  type AlarmRule,
  type AlarmRuleWritePayload,
  type AlarmStatusFilter,
} from '@/services/alarmApi';

const POLL_INTERVAL_MS = 15_000;

const SEVERITY_COLORS: Record<string, string> = {
  info: 'blue',
  warning: 'gold',
  critical: 'red',
};

const CATEGORY_LABEL: Record<string, string> = {
  threshold: '阈值',
  connectivity: '连接',
  system: '系统',
  lifecycle: '生命周期',
};

const STATUS_META: Record<
  string,
  { label: string; badge: 'error' | 'warning' | 'success' | 'default' }
> = {
  firing: { label: '未确认', badge: 'error' },
  acked: { label: '已确认', badge: 'warning' },
  cleared: { label: '已恢复', badge: 'success' },
};

const OPERATOR_LABEL: Record<string, string> = {
  gt: '>',
  ge: '≥',
  lt: '<',
  le: '≤',
  eq: '=',
  ne: '≠',
  between: '∈',
  outside: '∉',
};

const statusOptions = [
  { label: '全部', value: 'all' },
  { label: '未确认', value: 'firing' },
  { label: '已确认', value: 'acked' },
  { label: '已恢复', value: 'cleared' },
];

const operatorOptions = [
  { label: '> 大于', value: 'gt' },
  { label: '≥ 大于等于', value: 'ge' },
  { label: '< 小于', value: 'lt' },
  { label: '≤ 小于等于', value: 'le' },
  { label: '= 等于', value: 'eq' },
  { label: '≠ 不等于', value: 'ne' },
  { label: '∈ 区间内', value: 'between' },
  { label: '∉ 区间外', value: 'outside' },
];

const severityOptions = [
  { label: '🔵 info 提示', value: 'info' },
  { label: '🟡 warning 警告', value: 'warning' },
  { label: '🔴 critical 严重', value: 'critical' },
];

const alarmColumns: TableColumnsType = [
  { title: '触发时间', dataIndex: 'fired_at', key: 'fired_at', width: 180 },
  { title: '严重度', dataIndex: 'severity', key: 'severity', width: 90 },
  { title: '类别', dataIndex: 'category', key: 'category', width: 90 },
  { title: '规则 / 说明', key: 'description', minWidth: 160 },
  { title: '设备', dataIndex: 'device_code', key: 'device_code' },
  { title: '测点', dataIndex: 'point_code', key: 'point_code' },
  { title: '触发值', dataIndex: 'value', key: 'value' },
  { title: '状态', dataIndex: 'status', key: 'status', width: 100 },
  { title: '操作', key: 'action', width: 100 },
];

const ruleColumns: TableColumnsType = [
  { title: '名称', dataIndex: 'name', key: 'name' },
  { title: '匹配', key: 'match' },
  { title: '条件', key: 'condition' },
  { title: '严重度', dataIndex: 'severity', key: 'severity', width: 90 },
  { title: '启用', dataIndex: 'is_active', key: 'is_active', width: 80 },
  { title: '操作', key: 'action', width: 140, align: 'right' },
];

type AlarmRuleForm = Omit<AlarmRuleWritePayload, 'threshold' | 'threshold_high'> & {
  threshold: number | undefined;
  threshold_high: number | undefined;
};

function blankRule(): AlarmRuleForm {
  return {
    name: '',
    point_code: '',
    device_code: '',
    operator: 'gt',
    threshold: undefined,
    threshold_high: undefined,
    severity: 'warning',
    is_active: true,
    description: '',
  };
}

const { message, modal } = AntApp.useApp();
const alarms = ref<AlarmRecord[]>([]);
const rules = ref<AlarmRule[]>([]);
const alarmLoading = ref(true);
const ruleLoading = ref(true);
const statusFilter = ref<AlarmStatusFilter>('all');
const activeTab = ref<'alarms' | 'rules'>('alarms');
const alarmErrorMessage = ref<string | null>(null);
const ruleErrorMessage = ref<string | null>(null);
const actionErrorMessage = ref<string | null>(null);
const acknowledgingId = ref<number | null>(null);
const deletingRuleId = ref<number | null>(null);
const savingRule = ref(false);
const ruleModalOpen = ref(false);
const editingRule = ref<AlarmRule | null>(null);
const ruleFormRef = ref<FormInstance>();
const ruleForm = reactive<AlarmRuleForm>(blankRule());
const mutationController = new AbortController();

let loadController: AbortController | null = null;
let pollTimer: ReturnType<typeof setInterval> | null = null;

const loading = computed(() => alarmLoading.value || ruleLoading.value);
const firingCount = computed(
  () => alarms.value.filter((alarm) => alarm.status === 'firing').length,
);
const rangeOperator = computed(() => isRangeAlarmOperator(ruleForm.operator));

function statusMeta(status: string) {
  return STATUS_META[status] ?? { label: status || '未知', badge: 'default' as const };
}

function categoryLabel(category?: string): string {
  return CATEGORY_LABEL[category ?? 'threshold'] ?? category ?? '阈值';
}

function formatAlarmValue(value: unknown): string {
  if (value == null) return '—';
  if (typeof value === 'object') {
    return Object.keys(value as Record<string, unknown>).length === 0
      ? '—'
      : JSON.stringify(value);
  }
  return String(value);
}

function formatTime(value: string): string {
  return new Date(value).toLocaleString('zh-CN');
}

function formatRuleCondition(rule: AlarmRule): string {
  const operator = OPERATOR_LABEL[rule.operator] ?? rule.operator;
  if (rule.operator === 'between' || rule.operator === 'outside') {
    return `${operator} [${rule.threshold}, ${rule.threshold_high}]`;
  }
  return `${operator} ${rule.threshold}`;
}

function asAlarmRule(record: Record<string, unknown>): AlarmRule {
  return record as unknown as AlarmRule;
}

function errorDetail(error: unknown): string {
  return error instanceof Error ? error.message : '未知错误';
}

function validateThresholdHigh(): Promise<void> {
  const validationError = alarmRuleRangeError(rulePayload());
  if (!validationError || validationError === '区间规则必须填写阈值下限') {
    return Promise.resolve();
  }
  return Promise.reject(new Error(validationError));
}

async function refresh(): Promise<void> {
  loadController?.abort();
  const request = new AbortController();
  loadController = request;
  alarmLoading.value = true;
  ruleLoading.value = true;
  alarmErrorMessage.value = null;
  ruleErrorMessage.value = null;

  const isCurrentRequest = () => !request.signal.aborted && loadController === request;
  const alarmRequest = fetchAlarms(statusFilter.value, request.signal)
    .then((records) => {
      if (isCurrentRequest()) alarms.value = records;
    })
    .catch((error: unknown) => {
      if (!isCurrentRequest()) return;
      alarms.value = [];
      alarmErrorMessage.value = `告警记录加载失败：${errorDetail(error)}`;
    })
    .finally(() => {
      if (isCurrentRequest()) alarmLoading.value = false;
    });
  const ruleRequest = fetchAlarmRules(request.signal)
    .then((alarmRules) => {
      if (isCurrentRequest()) rules.value = alarmRules;
    })
    .catch((error: unknown) => {
      if (!isCurrentRequest()) return;
      rules.value = [];
      ruleErrorMessage.value = `告警规则加载失败：${errorDetail(error)}`;
    })
    .finally(() => {
      if (isCurrentRequest()) ruleLoading.value = false;
    });

  await Promise.allSettled([alarmRequest, ruleRequest]);
}

async function ackAlarm(id: number): Promise<void> {
  actionErrorMessage.value = null;
  acknowledgingId.value = id;
  try {
    await acknowledgeAlarm(id, mutationController.signal);
    message.success('已确认');
    await refresh();
  } catch (error) {
    if (!mutationController.signal.aborted) {
      actionErrorMessage.value = `确认告警失败：${errorDetail(error)}`;
    }
  } finally {
    if (!mutationController.signal.aborted) acknowledgingId.value = null;
  }
}

function openRuleModal(rule?: AlarmRule): void {
  editingRule.value = rule ?? null;
  Object.assign(
    ruleForm,
    blankRule(),
    rule
      ? {
          ...rule,
          threshold: rule.threshold ?? undefined,
          threshold_high: rule.threshold_high ?? undefined,
        }
      : {},
  );
  ruleModalOpen.value = true;
  void nextTick(() => ruleFormRef.value?.clearValidate());
}

function closeRuleModal(): void {
  ruleModalOpen.value = false;
  editingRule.value = null;
}

function rulePayload(): AlarmRuleWritePayload {
  return {
    name: ruleForm.name,
    point_code: ruleForm.point_code,
    device_code: ruleForm.device_code,
    operator: ruleForm.operator,
    threshold: ruleForm.threshold ?? null,
    threshold_high: ruleForm.threshold_high ?? null,
    severity: ruleForm.severity,
    is_active: ruleForm.is_active,
    description: ruleForm.description,
  };
}

async function saveRule(): Promise<void> {
  try {
    await ruleFormRef.value?.validate();
  } catch {
    return;
  }

  actionErrorMessage.value = null;
  savingRule.value = true;
  try {
    if (editingRule.value) {
      await updateAlarmRule(
        editingRule.value.id,
        rulePayload(),
        mutationController.signal,
      );
      message.success('规则已更新');
    } else {
      await createAlarmRule(rulePayload(), mutationController.signal);
      message.success('规则已创建');
    }
    closeRuleModal();
    await refresh();
  } catch (error) {
    if (!mutationController.signal.aborted) {
      actionErrorMessage.value = `保存规则失败：${errorDetail(error)}`;
    }
  } finally {
    if (!mutationController.signal.aborted) savingRule.value = false;
  }
}

async function performDeleteRule(rule: AlarmRule): Promise<void> {
  actionErrorMessage.value = null;
  deletingRuleId.value = rule.id;
  try {
    await deleteAlarmRule(rule.id, mutationController.signal);
    message.success('已删除');
    await refresh();
  } catch (error) {
    if (!mutationController.signal.aborted) {
      actionErrorMessage.value = `删除规则失败：${errorDetail(error)}`;
      throw error;
    }
  } finally {
    if (!mutationController.signal.aborted) deletingRuleId.value = null;
  }
}

function confirmDeleteRule(rule: AlarmRule): void {
  modal.confirm({
    title: `删除规则 "${rule.name}" ?`,
    content: '已产生的告警记录会保留。',
    okType: 'danger',
    onOk: () => performDeleteRule(rule),
  });
}

watch(statusFilter, () => {
  // A new filter is a new result set. Never label rows from the previous
  // filter as if they belonged to the new one while the request is pending.
  alarms.value = [];
  alarmErrorMessage.value = null;
  void refresh();
});

onMounted(() => {
  void refresh();
  pollTimer = setInterval(() => void refresh(), POLL_INTERVAL_MS);
});

onBeforeUnmount(() => {
  loadController?.abort();
  mutationController.abort();
  if (pollTimer) clearInterval(pollTimer);
});
</script>

<template>
  <div class="alarms-page" data-testid="alarms-page" :aria-busy="loading">
    <ACard :bordered="false" class="page-header-card">
      <div class="page-heading">
        <div>
          <ATypographyTitle :level="3" class="page-title">告警中心</ATypographyTitle>
          <ATypographyText type="secondary">
            基于阈值规则的实时告警。规则由采集循环持续评估，触发后写入记录并推送 WebSocket。
          </ATypographyText>
        </div>
        <ASpace>
          <AButton aria-label="刷新告警" :loading="loading" @click="refresh">
            <template #icon><ReloadOutlined /></template>
            刷新
          </AButton>
          <AButton
            v-if="activeTab === 'rules'"
            type="primary"
            aria-label="新建规则"
            @click="openRuleModal()"
          >
            <template #icon><PlusOutlined /></template>
            新建规则
          </AButton>
        </ASpace>
      </div>
    </ACard>

    <AAlert
      v-if="alarmErrorMessage"
      class="page-alert"
      type="error"
      show-icon
      :message="alarmErrorMessage"
    />

    <AAlert
      v-if="ruleErrorMessage"
      class="page-alert"
      type="error"
      show-icon
      :message="ruleErrorMessage"
    />

    <AAlert
      v-if="actionErrorMessage"
      class="page-alert"
      type="error"
      show-icon
      :message="actionErrorMessage"
    />

    <AAlert
      v-if="firingCount > 0 && activeTab === 'alarms'"
      class="page-alert"
      type="error"
      show-icon
      :message="`当前有 ${firingCount} 个未确认告警`"
    />

    <ACard :bordered="false">
      <ATabs v-model:active-key="activeTab">
        <ATabPane key="alarms" :tab="`告警记录 (${alarms.length})`">
          <ASegmented
            v-model:value="statusFilter"
            class="status-filter"
            :options="statusOptions"
          />
          <ATable
            row-key="id"
            :columns="alarmColumns"
            :data-source="alarms"
            :loading="alarmLoading"
            :pagination="{ pageSize: 15, hideOnSinglePage: true }"
            :locale="{ emptyText: '无告警' }"
            :scroll="{ x: 1100 }"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'fired_at'">
                {{ formatTime(record.fired_at) }}
              </template>
              <template v-else-if="column.key === 'severity'">
                <ATag :color="SEVERITY_COLORS[record.severity]">
                  {{ record.severity?.toUpperCase() }}
                </ATag>
              </template>
              <template v-else-if="column.key === 'category'">
                <ATag>{{ categoryLabel(record.category) }}</ATag>
              </template>
              <template v-else-if="column.key === 'description'">
                {{ record.rule_name || record.message || '—' }}
              </template>
              <template v-else-if="column.key === 'point_code'">
                <code v-if="record.point_code">{{ record.point_code }}</code>
                <span v-else>—</span>
              </template>
              <template v-else-if="column.key === 'value'">
                <strong>{{ formatAlarmValue(record.value) }}</strong>
              </template>
              <template v-else-if="column.key === 'status'">
                <ABadge
                  :status="statusMeta(record.status).badge"
                  :text="statusMeta(record.status).label"
                />
              </template>
              <template v-else-if="column.key === 'action'">
                <AButton
                  v-if="record.status === 'firing'"
                  size="small"
                  :aria-label="`确认告警 ${record.id}`"
                  :loading="acknowledgingId === record.id"
                  @click="ackAlarm(record.id)"
                >
                  <template #icon><CheckOutlined /></template>
                  确认
                </AButton>
              </template>
            </template>
          </ATable>
        </ATabPane>

        <ATabPane key="rules" :tab="`阈值规则 (${rules.length})`">
          <ATable
            row-key="id"
            :columns="ruleColumns"
            :data-source="rules"
            :loading="ruleLoading"
            :pagination="{ pageSize: 15, hideOnSinglePage: true }"
            :locale="{ emptyText: '尚未创建任何规则，点击右上「新建规则」开始' }"
            :scroll="{ x: 780 }"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'match'">
                <code>{{ record.device_code || '*' }}/{{ record.point_code }}</code>
              </template>
              <template v-else-if="column.key === 'condition'">
                {{ formatRuleCondition(asAlarmRule(record)) }}
              </template>
              <template v-else-if="column.key === 'severity'">
                <ATag :color="SEVERITY_COLORS[record.severity]">
                  {{ record.severity?.toUpperCase() }}
                </ATag>
              </template>
              <template v-else-if="column.key === 'is_active'">
                <ABadge
                  :status="record.is_active ? 'success' : 'default'"
                  :text="record.is_active ? '启用' : '停用'"
                />
              </template>
              <template v-else-if="column.key === 'action'">
                <ASpace size="small">
                  <AButton
                    size="small"
                    :aria-label="`编辑规则 ${record.name}`"
                    @click="openRuleModal(asAlarmRule(record))"
                  >
                    <template #icon><EditOutlined /></template>
                  </AButton>
                  <AButton
                    danger
                    size="small"
                    :aria-label="`删除规则 ${record.name}`"
                    :loading="deletingRuleId === record.id"
                    @click="confirmDeleteRule(asAlarmRule(record))"
                  >
                    <template #icon><DeleteOutlined /></template>
                  </AButton>
                </ASpace>
              </template>
            </template>
          </ATable>
        </ATabPane>
      </ATabs>
    </ACard>

    <AModal
      v-model:open="ruleModalOpen"
      :title="editingRule ? '编辑规则' : '新建规则'"
      ok-text="保存"
      cancel-text="取消"
      :confirm-loading="savingRule"
      :destroy-on-close="true"
      :width="640"
      @ok="saveRule"
      @cancel="closeRuleModal"
    >
      <AForm ref="ruleFormRef" :model="ruleForm" layout="vertical">
        <AFormItem
          name="name"
          label="规则名称"
          :rules="[{ required: true, message: '请输入规则名称' }]"
        >
          <AInput v-model:value="ruleForm.name" placeholder="例如：温度过高告警" />
        </AFormItem>

        <ARow :gutter="16">
          <ACol :xs="24" :md="12">
            <AFormItem
              name="point_code"
              label="测点编码"
              :rules="[{ required: true, message: '请输入测点编码' }]"
            >
              <AInput v-model:value="ruleForm.point_code" placeholder="例如：temperature" />
            </AFormItem>
          </ACol>
          <ACol :xs="24" :md="12">
            <AFormItem name="device_code" label="设备编码（留空匹配所有）">
              <AInput v-model:value="ruleForm.device_code" placeholder="选填" />
            </AFormItem>
          </ACol>
        </ARow>

        <ARow :gutter="16">
          <ACol :xs="24" :md="8">
            <AFormItem
              name="operator"
              label="操作符"
              :rules="[{ required: true, message: '请选择操作符' }]"
            >
              <ASelect v-model:value="ruleForm.operator" :options="operatorOptions" />
            </AFormItem>
          </ACol>
          <ACol :xs="24" :md="8">
            <AFormItem
              name="threshold"
              label="阈值"
              :required="ruleForm.is_active"
              :rules="ruleForm.is_active ? [{ required: true, message: '请输入阈值' }] : []"
            >
              <AInputNumber v-model:value="ruleForm.threshold" class="full-width" />
            </AFormItem>
          </ACol>
          <ACol :xs="24" :md="8">
            <AFormItem
              name="threshold_high"
              label="阈值上限（区间）"
              :required="ruleForm.is_active && rangeOperator"
              :rules="[{ validator: validateThresholdHigh, trigger: ['change', 'blur'] }]"
              extra="between/outside 必填，且不得小于阈值。"
            >
              <AInputNumber v-model:value="ruleForm.threshold_high" class="full-width" />
            </AFormItem>
          </ACol>
        </ARow>

        <ARow :gutter="16">
          <ACol :xs="24" :md="16">
            <AFormItem name="severity" label="严重度">
              <ASelect v-model:value="ruleForm.severity" :options="severityOptions" />
            </AFormItem>
          </ACol>
          <ACol :xs="24" :md="8">
            <AFormItem name="is_active" label="启用规则">
              <ASwitch v-model:checked="ruleForm.is_active" />
            </AFormItem>
          </ACol>
        </ARow>

        <AFormItem name="description" label="描述">
          <AInput.TextArea v-model:value="ruleForm.description" :rows="2" />
        </AFormItem>
      </AForm>
    </AModal>
  </div>
</template>

<style scoped>
.alarms-page {
  min-width: 0;
}

.page-header-card,
.page-alert {
  margin-bottom: 16px;
}

.page-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.page-title {
  margin: 0;
}

.status-filter {
  margin-bottom: 16px;
}

.full-width {
  width: 100%;
}

@media (max-width: 576px) {
  .page-heading {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
