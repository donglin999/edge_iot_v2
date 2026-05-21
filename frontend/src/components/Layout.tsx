import { useState } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import './Layout.css';

// SVG Icons as components for consistent rendering
const DashboardIcon = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <rect x="3" y="3" width="7" height="9" rx="1" />
    <rect x="14" y="3" width="7" height="5" rx="1" />
    <rect x="14" y="12" width="7" height="9" rx="1" />
    <rect x="3" y="16" width="7" height="5" rx="1" />
  </svg>
);

const AcquisitionIcon = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83" />
  </svg>
);

const DeviceIcon = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <rect x="4" y="4" width="16" height="16" rx="2" />
    <rect x="9" y="9" width="6" height="6" />
    <path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3" />
  </svg>
);

const DataIcon = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M3 3v18h18" />
    <path d="M18 17V9M13 17V5M8 17v-3" />
  </svg>
);

const ImportIcon = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" />
    <polyline points="17 8 12 3 7 8" />
    <line x1="12" y1="3" x2="12" y2="15" />
  </svg>
);

const VersionIcon = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <circle cx="12" cy="12" r="10" />
    <polyline points="12 6 12 12 16 14" />
  </svg>
);

const AlarmIcon = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
    <path d="M10 21a2 2 0 0 0 4 0" />
  </svg>
);

const FleetIcon = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    <rect x="3" y="4" width="18" height="6" rx="1" />
    <rect x="3" y="14" width="18" height="6" rx="1" />
    <circle cx="7" cy="7" r="0.8" fill="currentColor" />
    <circle cx="7" cy="17" r="0.8" fill="currentColor" />
  </svg>
);

const CollapseIcon = ({ collapsed }: { collapsed: boolean }) => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
    {collapsed ? (
      <polyline points="9 18 15 12 9 6" />
    ) : (
      <polyline points="15 18 9 12 15 6" />
    )}
  </svg>
);

interface NavItem {
  path: string;
  label: string;
  icon: React.ReactNode;
  end?: boolean;
}

const navItems: NavItem[] = [
  { path: '/', label: '任务总览', icon: <DashboardIcon />, end: true },
  { path: '/acquisition', label: '采集控制', icon: <AcquisitionIcon /> },
  { path: '/devices', label: '连接与测点', icon: <DeviceIcon /> },
  { path: '/fleet', label: '边缘节点', icon: <FleetIcon /> },
  { path: '/data', label: '数据可视化', icon: <DataIcon /> },
  { path: '/import', label: '导入作业', icon: <ImportIcon /> },
  { path: '/alarms', label: '告警中心', icon: <AlarmIcon /> },
  { path: '/versions', label: '版本历史', icon: <VersionIcon /> },
];

const routeTitles: Record<string, string> = {
  '/': '任务总览',
  '/acquisition': '采集控制',
  '/devices': '连接与测点',
  '/fleet': '边缘节点',
  '/data': '数据可视化',
  '/import': '导入作业',
  '/alarms': '告警中心',
  '/versions': '版本历史',
};

const Layout = () => {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const location = useLocation();

  // Get page title from current path
  const getPageTitle = () => {
    // Handle nested routes like /devices/:id
    const basePath = '/' + location.pathname.split('/')[1];
    return routeTitles[basePath] || routeTitles[location.pathname] || 'Edge IoT';
  };

  return (
    <div className={`layout ${sidebarCollapsed ? 'layout--collapsed' : ''}`}>
      {/* Sidebar */}
      <aside className="sidebar">
        <div className="sidebar__header">
          <div className="sidebar__logo">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none">
              <rect x="2" y="2" width="20" height="20" rx="4" fill="#1f7a8c" />
              <path d="M7 12h10M12 7v10" stroke="white" strokeWidth="2" strokeLinecap="round" />
            </svg>
            {!sidebarCollapsed && <span className="sidebar__title">Edge IoT</span>}
          </div>
          <button
            className="sidebar__toggle"
            onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
            aria-label={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}
          >
            <CollapseIcon collapsed={sidebarCollapsed} />
          </button>
        </div>

        <nav className="sidebar__nav">
          {navItems.map((item) => (
            <NavLink
              key={item.path}
              to={item.path}
              end={item.end}
              className={({ isActive }) =>
                `sidebar__link ${isActive ? 'sidebar__link--active' : ''}`
              }
              title={item.label}
            >
              <span className="sidebar__icon">{item.icon}</span>
              {!sidebarCollapsed && <span className="sidebar__label">{item.label}</span>}
            </NavLink>
          ))}
        </nav>

        <div className="sidebar__footer">
          {!sidebarCollapsed && (
            <div className="sidebar__status">
              <span className="status-dot status-dot--online" />
              <span className="sidebar__status-text">系统正常</span>
            </div>
          )}
        </div>
      </aside>

      {/* Main Content */}
      <main className="main">
        <header className="main__header">
          <h1 className="main__title">{getPageTitle()}</h1>
          <div className="main__actions">
            <span className="main__time">
              {new Date().toLocaleDateString('zh-CN', {
                year: 'numeric',
                month: 'long',
                day: 'numeric',
                weekday: 'long',
              })}
            </span>
          </div>
        </header>
        <div className="main__content">
          <Outlet />
        </div>
      </main>
    </div>
  );
};

export default Layout;
