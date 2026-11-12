import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { App as AntApp, ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import App from './App';
import 'antd/dist/reset.css';
import './styles.css';
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: '#326bd1',
          colorInfo: '#326bd1',
          colorBgLayout: '#f3f5f7',
          colorText: '#253448',
          colorTextSecondary: '#758294',
          borderRadius: 8,
          fontFamily: 'Inter, "Segoe UI", "Microsoft YaHei", sans-serif',
          fontSize: 13,
        },
        components: {
          Layout: { headerBg: '#ffffff', siderBg: '#162944' },
          Menu: { darkItemBg: '#162944', darkItemSelectedBg: '#29466d', darkItemColor: '#b9cae1' },
          Card: { headerFontSize: 15 },
        },
      }}
    >
      <AntApp>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </AntApp>
    </ConfigProvider>
  </React.StrictMode>,
);
