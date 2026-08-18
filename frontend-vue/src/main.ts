import 'ant-design-vue/dist/reset.css';
import '@/styles/index.css';

import { createApp } from 'vue';

import App from './App.vue';
import { antDesignPlugin } from './plugins/antDesign';
import { createAppRouter } from './router';
import { setApiErrorNotifier } from './services/apiClient';

const app = createApp(App);

app.use(antDesignPlugin);
app.use(createAppRouter());
setApiErrorNotifier((text) => {
  void app.config.globalProperties.$message.error(text);
});
app.mount('#app');
