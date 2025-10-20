import { NavLink, Route, Routes } from 'react-router-dom';
import DashboardPage from './pages/DashboardPage';
import ImportJobPage from './pages/ImportJobPage';
import DeviceListPage from './pages/DeviceListPage';
import DeviceDetailPage from './pages/DeviceDetailPage';
import AcquisitionControlPage from './pages/AcquisitionControlPage';
import VersionHistoryPage from './pages/VersionHistoryPage';
import DataVisualizationPage from './pages/DataVisualizationPage';

const App = () => {
  return (
    <div className="app">
      <header className="app__header">
        <h1>工业采集控制平台</h1>
        <nav className="app__nav">
          <NavLink to="/" end>任务总览</NavLink>
          <NavLink to="/acquisition">采集控制</NavLink>
          <NavLink to="/devices">连接与测点</NavLink>
          <NavLink to="/data">数据可视化</NavLink>
          <NavLink to="/import">导入作业</NavLink>
          <NavLink to="/versions">版本历史</NavLink>
        </nav>
      </header>
      <main className="app__content">
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/acquisition" element={<AcquisitionControlPage />} />
          <Route path="/devices" element={<DeviceListPage />} />
          <Route path="/devices/:id" element={<DeviceDetailPage />} />
          <Route path="/data" element={<DataVisualizationPage />} />
          <Route path="/import" element={<ImportJobPage />} />
          <Route path="/versions" element={<VersionHistoryPage />} />
        </Routes>
      </main>
    </div>
  );
};

export default App;
