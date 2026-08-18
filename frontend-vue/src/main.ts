import 'ant-design-vue/dist/reset.css';
import '@/styles/index.css';

import { createApp } from 'vue';

import App from './App.vue';
import { antDesignPlugin } from './plugins/antDesign';
import { createApiErrorNotifier } from './plugins/apiNotifications';
import { createAppRouter } from './router';
import { setApiErrorNotifier } from './services/apiClient';

const app = createApp(App);

app.use(antDesignPlugin);
app.use(createAppRouter());
setApiErrorNotifier(createApiErrorNotifier());
app.mount('#app');
