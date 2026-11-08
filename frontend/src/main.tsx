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
          colorPrimary: '#176d5c',
          colorInfo: '#176d5c',
          colorBgLayout: '#f3f5f7',
          colorText: '#253448',
          colorTextSecondary: '#758294',
          borderRadius: 8,
          fontFamily: 'Inter, "Segoe UI", "Microsoft YaHei", sans-serif',
          fontSize: 13,
        },
        components: {
          Layout: { headerBg: '#ffffff', siderBg: '#12312e' },
          Menu: { darkItemBg: '#12312e', darkItemSelectedBg: '#25544c', darkItemColor: '#bbcec9' },
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
