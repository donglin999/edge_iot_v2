<script setup lang="ts">
import {
  AlertOutlined,
  BarChartOutlined,
  CloudServerOutlined,
  ControlOutlined,
  DashboardOutlined,
  HistoryOutlined,
  ImportOutlined,
} from '@ant-design/icons-vue';
import type { ItemType } from 'ant-design-vue';
import { computed, h } from 'vue';
import { RouterView, useRoute, useRouter } from 'vue-router';

const route = useRoute();
const router = useRouter();

const menuItems: ItemType[] = [
  { key: '/', icon: () => h(DashboardOutlined), label: '概览' },
  { key: '/acquisition', icon: () => h(ControlOutlined), label: '采集控制' },
  { key: '/devices', icon: () => h(CloudServerOutlined), label: '设备管理' },
  { key: '/data', icon: () => h(BarChartOutlined), label: '数据可视化' },
  { key: '/import', icon: () => h(ImportOutlined), label: '配置导入' },
  { key: '/versions', icon: () => h(HistoryOutlined), label: '版本历史' },
  { key: '/alarms', icon: () => h(AlertOutlined), label: '告警中心' },
];

const selectedKeys = computed(() => {
  const matched = menuItems
    .map((item) => String(item?.key ?? ''))
    .filter((key) => key !== '/' && route.path.startsWith(key))
    .sort((left, right) => right.length - left.length)[0];
  return [matched || '/'];
});

function navigate({ key }: { key: string | number }): void {
  void router.push(String(key));
}
</script>

<template>
  <a-layout class="app-layout">
    <a-layout-sider class="app-sider" :width="220" breakpoint="lg" collapsed-width="0">
      <div class="app-brand">
        <span class="app-brand-mark">EI</span>
        <span>工业数据平台</span>
      </div>
      <a-menu
        mode="inline"
        theme="dark"
        :items="menuItems"
        :selected-keys="selectedKeys"
        @click="navigate"
      />
    </a-layout-sider>
    <a-layout>
      <a-layout-header class="app-header">
        <div>
          <div class="app-header-title">边缘采集控制台</div>
          <div class="app-header-subtitle">Vue 3 平行版本 · Django API</div>
        </div>
        <a-tag color="blue">M2</a-tag>
      </a-layout-header>
      <a-layout-content class="app-content">
        <Suspense>
          <RouterView />
          <template #fallback>
            <div class="route-loading" aria-label="页面加载中">
              <a-spin size="large" />
            </div>
          </template>
        </Suspense>
      </a-layout-content>
    </a-layout>
  </a-layout>
</template>
