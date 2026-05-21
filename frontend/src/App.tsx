import { ConfigProvider, App as AntdApp } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { Route, Routes } from 'react-router-dom';

import Layout from './components/Layout';
import DashboardPage from './pages/DashboardPage';
import ImportJobPage from './pages/ImportJobPage';
import DeviceListPage from './pages/DeviceListPage';
import DeviceDetailPage from './pages/DeviceDetailPage';
import AcquisitionControlPage from './pages/AcquisitionControlPage';
import VersionHistoryPage from './pages/VersionHistoryPage';
import DataVisualizationPage from './pages/DataVisualizationPage';
import AlarmsPage from './pages/AlarmsPage';
import FleetPage from './pages/FleetPage';

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
          <Route path="fleet" element={<FleetPage />} />
        </Route>
      </Routes>
    </AntdApp>
  </ConfigProvider>
);

export default App;
