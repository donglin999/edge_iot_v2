<script setup lang="ts">
import zhCN from 'ant-design-vue/es/locale/zh_CN';
import { RouterView } from 'vue-router';

import AppErrorBoundary from '@/components/AppErrorBoundary.vue';
</script>

<template>
  <a-config-provider
    :locale="zhCN"
    :theme="{
      token: {
        colorPrimary: '#1f6feb',
        borderRadius: 6,
        fontFamily:
          '-apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif',
      },
    }"
  >
    <a-app>
      <AppErrorBoundary>
        <RouterView />
      </AppErrorBoundary>
    </a-app>
  </a-config-provider>
</template>
