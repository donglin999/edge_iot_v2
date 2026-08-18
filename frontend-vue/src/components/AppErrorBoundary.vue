<script setup lang="ts">
import { onErrorCaptured, ref } from 'vue';

const error = ref<Error | null>(null);
const renderKey = ref(0);

onErrorCaptured((caught) => {
  error.value = caught instanceof Error ? caught : new Error(String(caught));
  return false;
});

function retry(): void {
  error.value = null;
  renderKey.value += 1;
}
</script>

<template>
  <a-result
    v-if="error"
    status="error"
    title="页面加载失败"
    sub-title="请重试；如果问题持续，请保留当前操作步骤并联系管理员。"
  >
    <template #extra>
      <a-button type="primary" @click="retry">重新加载页面</a-button>
    </template>
  </a-result>
  <div v-else :key="renderKey" class="app-boundary-content">
    <slot />
  </div>
</template>
