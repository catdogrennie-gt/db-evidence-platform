import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({plugins:[react()],server:{proxy:{'/api': 'http://127.0.0.1:18080'}},build:{chunkSizeWarningLimit:900,rollupOptions:{output:{manualChunks(id){if(id.includes('node_modules/echarts')||id.includes('node_modules/zrender'))return 'charts';if(id.includes('node_modules/antd')||id.includes('node_modules/@ant-design')||id.includes('node_modules/@rc-component'))return 'antd';if(id.includes('node_modules/react'))return 'react'}}}}});
