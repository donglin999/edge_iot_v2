import { lazy, Suspense } from 'react';
import { ConfigProvider, App as AntdApp, Spin } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { Route, Routes } from 'react-router-dom';

import Layout from './components/Layout';

// Route components are code-split via React.lazy so each page ships as its own
// chunk instead of inflating the single main bundle. Layout stays eager because
// it is the always-present app shell.
const DashboardPage = lazy(() => import('./pages/DashboardPage'));
const ImportJobPage = lazy(() => import('./pages/ImportJobPage'));
const DeviceListPage = lazy(() => import('./pages/DeviceListPage'));
const DeviceDetailPage = lazy(() => import('./pages/DeviceDetailPage'));
const AcquisitionControlPage = lazy(() => import('./pages/AcquisitionControlPage'));
const VersionHistoryPage = lazy(() => import('./pages/VersionHistoryPage'));
const DataVisualizationPage = lazy(() => import('./pages/DataVisualizationPage'));
const AlarmsPage = lazy(() => import('./pages/AlarmsPage'));

const RouteFallback = () => (
  <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '60vh' }}>
    <Spin size="large" />
  </div>
);

const App = () => (
  <ConfigProvider
    locale={zhCN}
    theme={{
      token: {
        colorPrimary: '#1f6feb',
        borderRadius: 6,
        fontFamily:
          '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif',
      },
    }}
  >
    <AntdApp>
      <Suspense fallback={<RouteFallback />}>
        <Routes>
          <Route path="/" element={<Layout />}>
            <Route index element={<DashboardPage />} />
            <Route path="acquisition" element={<AcquisitionControlPage />} />
            <Route path="devices" element={<DeviceListPage />} />
            <Route path="devices/:id" element={<DeviceDetailPage />} />
            <Route path="data" element={<DataVisualizationPage />} />
            <Route path="import" element={<ImportJobPage />} />
            <Route path="versions" element={<VersionHistoryPage />} />
            <Route path="alarms" element={<AlarmsPage />} />
          </Route>
        </Routes>
      </Suspense>
    </AntdApp>
  </ConfigProvider>
);

export default App;
