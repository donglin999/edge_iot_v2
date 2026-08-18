<script setup lang="ts">
import {
  ApiOutlined,
  CloudUploadOutlined,
  DatabaseOutlined,
  HistoryOutlined,
  LineChartOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  RightOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons-vue';
import {
  Alert as AAlert,
  Button as AButton,
  Card as ACard,
  Empty as AEmpty,
  Statistic as AStatistic,
  Table as ATable,
  Tag as ATag,
  TypographyText as ATypographyText,
  TypographyTitle as ATypographyTitle,
  type TableColumnsType,
} from 'ant-design-vue';
import type { Component } from 'vue';
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { RouterLink } from 'vue-router';

import {
  fetchDashboardActiveSessions,
  fetchDashboardDevices,
  fetchDashboardOverview,
  fetchDashboardTasks,
  type DashboardOverview,
  type DashboardTask,
  type DashboardTaskRun,
} from '@/services/dashboardApi';

const POLL_INTERVAL_MS = 30_000;

const RUN_STATUS: Record<string, { text: string; color: string }> = {
  running: { text: '运行中', color: 'processing' },
  succeeded: { text: '成功', color: 'success' },
  completed: { text: '完成', color: 'success' },
  stopped: { text: '已停止', color: 'default' },
  failed: { text: '失败', color: 'error' },
  error: { text: '错误', color: 'error' },
};

const overview = ref<DashboardOverview | null>(null);
const tasks = ref<DashboardTask[]>([]);
const deviceStats = ref({ total: 0, online: 0 });
const runningCount = ref(0);
const initialLoading = ref(true);
const refreshing = ref(false);
const errorMessage = ref<string | null>(null);

let activeRequest: AbortController | null = null;
let pollTimer: ReturnType<typeof setInterval> | null = null;

const taskColumns: TableColumnsType = [
  { title: '任务编码', dataIndex: 'code', key: 'code' },
  { title: '名称', dataIndex: 'name', key: 'name' },
  { title: '状态', dataIndex: 'is_active', key: 'is_active', width: 90 },
  { title: '', key: 'action', width: 80 },
];

const runColumns: TableColumnsType = [
  { title: '任务', dataIndex: 'task', key: 'task' },
  { title: '状态', dataIndex: 'status', key: 'status', width: 100 },
  { title: '时间', dataIndex: 'started_at', key: 'started_at', width: 150 },
];

interface QuickAction {
  to: string;
  icon: Component;
  title: string;
  description: string;
}

const quickActions: QuickAction[] = [
  { to: '/acquisition', icon: PlayCircleOutlined, title: '采集控制', description: '启停任务、管理测点' },
  { to: '/devices', icon: DatabaseOutlined, title: '设备管理', description: '设备与测点配置' },
  { to: '/import', icon: CloudUploadOutlined, title: '导入配置', description: '上传 Excel 配置' },
  { to: '/data', icon: LineChartOutlined, title: '数据可视化', description: '历史趋势查询' },
  { to: '/alarms', icon: ThunderboltOutlined, title: '告警中心', description: '连接/系统告警' },
  { to: '/versions', icon: HistoryOutlined, title: '版本历史', description: '配置版本记录' },
];

function runStatus(status: string): { text: string; color: string } {
  return RUN_STATUS[status?.toLowerCase()] ?? { text: status || '未知', color: 'default' };
}

function formatTime(value: string | null): string {
  if (!value) return '-';
  return new Date(value).toLocaleString('zh-CN', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

async function loadDashboard(): Promise<void> {
  activeRequest?.abort();
  const request = new AbortController();
  activeRequest = request;
  refreshing.value = true;

  const [overviewResult, tasksResult, devicesResult, sessionsResult] =
    await Promise.allSettled([
      fetchDashboardOverview(request.signal),
      fetchDashboardTasks(request.signal),
      fetchDashboardDevices(request.signal),
      fetchDashboardActiveSessions(request.signal),
    ]);

  if (request.signal.aborted || activeRequest !== request) return;

  const failed: string[] = [];
  if (overviewResult.status === 'fulfilled') overview.value = overviewResult.value;
  else failed.push('任务概览');

  if (tasksResult.status === 'fulfilled') tasks.value = tasksResult.value;
  else failed.push('任务列表');

  const devices = devicesResult.status === 'fulfilled' ? devicesResult.value : [];
  if (devicesResult.status === 'rejected') failed.push('设备');
  deviceStats.value = {
    total: devices.length,
    online: devices.filter((device) => device.status === 'online').length,
  };

  const sessions = sessionsResult.status === 'fulfilled' ? sessionsResult.value : [];
  if (sessionsResult.status === 'rejected') failed.push('活跃会话');
  runningCount.value = sessions.filter((session) => session.status === 'running').length;

  errorMessage.value = failed.length > 0
    ? `部分数据加载失败：${failed.join('、')}`
    : null;
  initialLoading.value = false;
  refreshing.value = false;
}

onMounted(() => {
  void loadDashboard();
  pollTimer = setInterval(() => void loadDashboard(), POLL_INTERVAL_MS);
});

onBeforeUnmount(() => {
  activeRequest?.abort();
  if (pollTimer) clearInterval(pollTimer);
});
</script>

<template>
  <div
    class="dashboard-page"
    data-testid="dashboard-page"
    :aria-busy="initialLoading"
  >
    <div class="page-heading">
      <div>
        <div class="page-kicker">系统概览</div>
        <ATypographyTitle :level="3" class="page-title">数据采集平台</ATypographyTitle>
        <ATypographyText type="secondary">
          任务概览与最近运行：默认站点（site_code=default）；任务列表、设备与运行会话：全局接口口径。
        </ATypographyText>
      </div>
      <AButton
        aria-label="刷新概览"
        :loading="refreshing && !initialLoading"
        @click="loadDashboard"
      >
        <template #icon><ReloadOutlined /></template>
        刷新
      </AButton>
    </div>

    <AAlert
      v-if="errorMessage"
      class="dashboard-alert"
      type="warning"
      show-icon
      :message="errorMessage"
    />

    <div class="metric-grid dashboard-section">
      <div>
        <ACard :bordered="false" :loading="initialLoading">
          <AStatistic title="默认站点任务总数" :value="overview?.total_tasks ?? 0">
            <template #prefix><DatabaseOutlined /></template>
          </AStatistic>
        </ACard>
      </div>
      <div>
        <ACard :bordered="false" :loading="initialLoading">
          <AStatistic title="默认站点启用任务" :value="overview?.active_tasks ?? 0" />
        </ACard>
      </div>
      <div>
        <ACard :bordered="false" :loading="initialLoading">
          <AStatistic
            title="全局运行中会话"
            :value="runningCount"
            :value-style="{ color: runningCount > 0 ? '#52c41a' : undefined }"
          >
            <template #prefix><PlayCircleOutlined /></template>
          </AStatistic>
        </ACard>
      </div>
      <div>
        <ACard :bordered="false" :loading="initialLoading">
          <AStatistic
            title="全局设备在线"
            :value="deviceStats.online"
            :suffix="`/ ${deviceStats.total}`"
            :value-style="{
              color: deviceStats.total > 0 && deviceStats.online === 0 ? '#ff4d4f' : undefined,
            }"
          >
            <template #prefix><ApiOutlined /></template>
          </AStatistic>
        </ACard>
      </div>
    </div>

    <div class="content-grid dashboard-section">
      <div>
        <ACard :bordered="false" title="全局任务列表">
          <template #extra>
            <RouterLink to="/acquisition">采集控制 <RightOutlined /></RouterLink>
          </template>
          <AEmpty
            v-if="!initialLoading && tasks.length === 0"
            description="暂无任务 —— 导入配置或在采集控制页新建"
          />
          <ATable
            v-else
            row-key="id"
            size="small"
            :columns="taskColumns"
            :data-source="tasks.slice(0, 6)"
            :pagination="false"
            :loading="initialLoading"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'code'">
                <code>{{ record.code }}</code>
              </template>
              <template v-else-if="column.key === 'is_active'">
                <ATag :color="record.is_active ? 'success' : 'default'">
                  {{ record.is_active ? '启用' : '停用' }}
                </ATag>
              </template>
              <template v-else-if="column.key === 'action'">
                <RouterLink to="/acquisition"><PlayCircleOutlined /> 控制</RouterLink>
              </template>
            </template>
          </ATable>
        </ACard>
      </div>

      <div>
        <ACard :bordered="false" title="默认站点最近运行">
          <AEmpty
            v-if="!initialLoading && !overview?.recent_runs.length"
            description="暂无运行记录 —— 启动采集任务后在此显示"
          />
          <ATable
            v-else
            :row-key="(record: DashboardTaskRun) => record.log_reference || `${record.task}|${record.started_at || ''}`"
            size="small"
            :columns="runColumns"
            :data-source="overview?.recent_runs.slice(0, 6) ?? []"
            :pagination="false"
            :loading="initialLoading"
          >
            <template #bodyCell="{ column, record }">
              <template v-if="column.key === 'task'">
                <div>{{ record.task }}</div>
                <ATypographyText type="secondary" class="table-note">
                  {{ record.worker || '本地' }}
                </ATypographyText>
              </template>
              <template v-else-if="column.key === 'status'">
                <ATag :color="runStatus(record.status).color">
                  {{ runStatus(record.status).text }}
                </ATag>
              </template>
              <template v-else-if="column.key === 'started_at'">
                <ATypographyText type="secondary">
                  {{ formatTime(record.started_at) }}
                </ATypographyText>
              </template>
            </template>
          </ATable>
        </ACard>
      </div>
    </div>

    <ACard :bordered="false" title="快速操作">
      <div class="quick-action-grid">
        <RouterLink
          v-for="action in quickActions"
          :key="action.to"
          :to="action.to"
          class="quick-action-link"
        >
          <ACard size="small" hoverable class="quick-action-card">
            <component :is="action.icon" class="quick-action-icon" />
            <div class="quick-action-title">{{ action.title }}</div>
            <ATypographyText type="secondary" class="table-note">
              {{ action.description }}
            </ATypographyText>
          </ACard>
        </RouterLink>
      </div>
    </ACard>
  </div>
</template>

<style scoped>
.dashboard-page {
  min-width: 0;
}

.page-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}

.page-title {
  margin: 0;
}

.page-kicker {
  margin-bottom: 2px;
  color: #6e7781;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.08em;
}

.dashboard-alert,
.dashboard-section {
  margin-bottom: 16px;
}

.table-note {
  font-size: 12px;
}

.metric-grid,
.content-grid,
.quick-action-grid {
  display: grid;
  gap: 16px;
}

.metric-grid {
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.content-grid {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.quick-action-grid {
  grid-template-columns: repeat(6, minmax(0, 1fr));
}

.quick-action-link {
  color: inherit;
}

.quick-action-card {
  height: 100%;
  text-align: center;
}

.quick-action-icon {
  margin-bottom: 6px;
  font-size: 22px;
}

.quick-action-title {
  font-weight: 600;
}

@media (max-width: 576px) {
  .page-heading {
    align-items: stretch;
    flex-direction: column;
  }

  .metric-grid,
  .quick-action-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (min-width: 577px) and (max-width: 991px) {
  .metric-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .quick-action-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 991px) {
  .content-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
