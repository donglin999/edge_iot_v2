import {
  createRouter,
  createWebHistory,
  type Router,
  type RouterHistory,
  type RouteRecordRaw,
} from 'vue-router';

import AppLayout from '@/layouts/AppLayout.vue';

export const APP_ROUTES: RouteRecordRaw[] = [
  {
    path: '/',
    component: AppLayout,
    children: [
      { path: '', name: 'dashboard', component: () => import('@/pages/DashboardPage.vue') },
      {
        path: 'acquisition',
        name: 'acquisition',
        component: () => import('@/pages/AcquisitionControlPage.vue'),
      },
      { path: 'devices', name: 'devices', component: () => import('@/pages/DeviceListPage.vue') },
      {
        path: 'devices/:id',
        name: 'device-detail',
        component: () => import('@/pages/DeviceDetailPage.vue'),
      },
      { path: 'data', name: 'data', component: () => import('@/pages/DataVisualizationPage.vue') },
      { path: 'import', name: 'import', component: () => import('@/pages/ImportJobPage.vue') },
      { path: 'versions', name: 'versions', component: () => import('@/pages/VersionHistoryPage.vue') },
      { path: 'alarms', name: 'alarms', component: () => import('@/pages/AlarmsPage.vue') },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
];

export function createAppRouter(
  history: RouterHistory = createWebHistory(import.meta.env.BASE_URL),
): Router {
  return createRouter({
    history,
    routes: APP_ROUTES,
    scrollBehavior: () => ({ top: 0 }),
  });
}
