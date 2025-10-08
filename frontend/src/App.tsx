import { NavLink, Route, Routes } from 'react-router-dom';
import DashboardPage from './pages/DashboardPage';
import ImportJobPage from './pages/ImportJobPage';
import DeviceListPage from './pages/DeviceListPage';

const App = () => {
  return (
    <div className="app">
      <header className="app__header">
        <h1>工业采集控制平台</h1>
        <nav className="app__nav">
          <NavLink to="/" end>任务总览</NavLink>
          <NavLink to="/devices">连接与测点</NavLink>
          <NavLink to="/import">导入作业</NavLink>
        </nav>
      </header>
      <main className="app__content">
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/devices" element={<DeviceListPage />} />
          <Route path="/import" element={<ImportJobPage />} />
        </Routes>
      </main>
    </div>
  );
};

export default App;
