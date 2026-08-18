<script setup lang="ts">
defineProps<{
  title: string;
  description: string;
}>();
</script>

<template>
  <a-card :bordered="false" class="route-placeholder">
    <a-result status="info" :title="title" :sub-title="description">
      <template #extra>
        <a-tag color="processing">Vue 3 平行迁移中</a-tag>
      </template>
    </a-result>
  </a-card>
</template>
