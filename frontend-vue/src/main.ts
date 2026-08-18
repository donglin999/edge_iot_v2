import 'ant-design-vue/dist/reset.css';
import '@/styles/index.css';

import { createApp } from 'vue';

import App from './App.vue';
import { antDesignPlugin } from './plugins/antDesign';
import { createAppRouter } from './router';

const app = createApp(App);

app.use(antDesignPlugin);
app.use(createAppRouter());
app.mount('#app');
