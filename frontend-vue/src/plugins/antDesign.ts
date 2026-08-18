import {
  App as AntApp,
  Button,
  Card,
  ConfigProvider,
  Layout,
  Menu,
  Result,
  Spin,
  Tag,
} from 'ant-design-vue';
import type { App, Plugin } from 'vue';

const components: Plugin[] = [
  AntApp,
  Button,
  Card,
  ConfigProvider,
  Layout,
  Menu,
  Result,
  Spin,
  Tag,
];

export const antDesignPlugin: Plugin = {
  install(app: App): void {
    for (const component of components) {
      app.use(component);
    }
  },
};
