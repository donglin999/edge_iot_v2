/**
 * 浏览器冒烟:按协议分发的配置界面 + scada 两表 Excel。
 *
 * 单测(vitest)把 apiClient 整个 mock 掉了,所以有一类 bug 它天生看不见 ——
 * 比如 FormData 撞上 apiClient 默认的 application/json 头、被 DRF 拒成 415。
 * 这个脚本打真浏览器、连真后端,专门补那一层。
 *
 * 跑法(需要本地已起 Django 和 vite):
 *   cd backend && python manage.py runserver 127.0.0.1:8000
 *   cd frontend && VITE_PROXY_TARGET=http://127.0.0.1:8000 npm run dev
 *   cd frontend && npm i --no-save @playwright/test && npx playwright install chromium
 *   node e2e/protocol-config.smoke.mjs
 *
 * 会真的建设备、真的导入 Excel,所以请对着一个可以随便写的库跑,别指生产。
 */
import { chromium } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';

const BASE = 'http://127.0.0.1:5173';
const SHOT = process.env.SMOKE_SHOTS || '/tmp/edge-iot-smoke';
const results = [];
mkdirSync(SHOT, { recursive: true });
const ok = (n, d = '') => { results.push(['PASS', n, d]); console.log('PASS', n, d); };
const bad = (n, d = '') => { results.push(['FAIL', n, d]); console.log('FAIL', n, d); };

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1500, height: 980 } });
const page = await ctx.newPage();
const errs = [];
page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));

// ---- 1. 侧边栏不再有 SCADA 网关 ----
await page.goto(BASE + '/devices', { waitUntil: 'networkidle' });
await page.waitForTimeout(800);
const navText = await page.locator('nav, aside').first().innerText().catch(() => '');
if (!/SCADA/i.test(navText)) ok('侧边栏无 SCADA 网关入口');
else bad('侧边栏仍有 SCADA 网关入口', navText.replace(/\n/g, '|'));

// ---- 2. /scada 路由已移除 ----
await page.goto(BASE + '/scada', { waitUntil: 'networkidle' });
await page.waitForTimeout(600);
const scadaBody = await page.locator('body').innerText();
if (!/网关服务配置|MQTT 连接参数/.test(scadaBody)) ok('/scada 路由已移除');
else bad('/scada 仍可访问');

// ---- 3. 添加设备 → scada 走专属界面 ----
await page.goto(BASE + '/devices', { waitUntil: 'networkidle' });
await page.waitForTimeout(800);
await page.getByRole('button', { name: /添加设备/ }).click();
await page.waitForTimeout(600);
await page.locator('.ant-dropdown-menu-item', { hasText: 'SCADA 网关' }).first().click();
await page.waitForTimeout(1500);
await page.screenshot({ path: `${SHOT}/01-create-scada.png` });
let modal = await page.locator('.ant-modal').innerText();
if (/网关/.test(modal) && /device_name|设备名/.test(modal)) ok('scada 走专属配置界面');
else bad('scada 未走专属界面', modal.slice(0, 300).replace(/\n/g, '|'));
// 基础字段(设备名称/站点 ID)不该由宿主再渲染一遍
if (!/站点 ID/.test(modal) || /网关服务/.test(modal)) ok('scada 隐藏了宿主公共字段(ownsBaseFields)');
else bad('scada 仍渲染宿主公共字段');
// 批量能力
const batchOk = /添加设备|复制本设备|应用到全部/.test(modal);
if (batchOk) ok('批量操作按钮保留');
else bad('批量操作按钮丢失');

// 实际下发一台设备
const stamp = String(Date.now()).slice(-6);
const dn = `UITEST${stamp}`;
const nameInputs = page.locator('.ant-modal input');
// device_name 输入框:placeholder 含 A0201 样例
const dnInput = page.locator('.ant-modal input[placeholder*="A0201"]').first();
if (await dnInput.count()) {
  await dnInput.fill(dn);
  const codeInput = page.locator('.ant-modal input[placeholder*="N2704"]').first();
  await codeInput.fill('N270400150027');
  const descInput = page.locator('.ant-modal input[placeholder*="注射压力"]').first();
  await descInput.fill('注射压力实际值');
  await page.getByRole('button', { name: /保\s*存|确\s*定/ }).last().click();
  await page.waitForTimeout(2500);
  await page.screenshot({ path: `${SHOT}/02-after-scada-save.png` });
  const modalGone = (await page.locator('.ant-modal-content').count()) === 0
    || !(await page.locator('.ant-modal-content').first().isVisible().catch(() => false));
  // 列表分页,设备一多新建的就翻到第二页去了 —— 直接问后端更可靠。
  const all = await (await fetch('http://127.0.0.1:8000/api/config/devices/?limit=500')).json();
  const created = (all.results ?? all).some((d) => (d.code || '').includes(dn));
  if (modalGone && created) ok('scada 新建设备已落库', dn);
  else bad('scada 新建设备保存失败', `modalGone=${modalGone} created=${created}`);
} else {
  bad('找不到 device_name 输入框', `inputs=${await nameInputs.count()}`);
}

// ---- 4. 添加设备 → modbus_tcp 走通用分组表单 ----
await page.goto(BASE + '/devices', { waitUntil: 'networkidle' });
await page.waitForTimeout(800);
await page.getByRole('button', { name: /添加设备/ }).click();
await page.waitForTimeout(600);
await page.locator('.ant-dropdown-menu-item', { hasText: 'Modbus TCP' }).first().click();
await page.waitForTimeout(1200);
await page.screenshot({ path: `${SHOT}/03-create-modbus.png` });
modal = await page.locator('.ant-modal').innerText();
if (/设备名称/.test(modal) && /站点 ID/.test(modal)) ok('通用协议保留宿主公共字段');
else bad('通用协议丢失公共字段');
if (/连接参数/.test(modal)) ok('通用表单已分组(连接参数)');
else bad('通用表单未分组', modal.slice(0, 300).replace(/\n/g, '|'));
if (/高级选项/.test(modal)) ok('高级选项折叠段存在');
else bad('无高级选项折叠段');

// 真建一台 modbus 设备,确认通用保存路径没坏
await page.locator('.ant-modal input#name, .ant-modal input[placeholder*="空压机"]').first().fill(`UI压机${stamp}`);
const ipInput = page.locator('.ant-modal input[id*="source_ip"]').first();
if (await ipInput.count()) {
  await ipInput.fill(`10.9.${(Number(stamp) % 250) + 1}.5`);
  await page.getByRole('button', { name: /保\s*存|确\s*定/ }).last().click();
  await page.waitForTimeout(2000);
  await page.screenshot({ path: `${SHOT}/04-after-modbus-save.png` });
  const all2 = await (await fetch('http://127.0.0.1:8000/api/config/devices/?limit=500')).json();
  if ((all2.results ?? all2).some((d) => d.name === `UI压机${stamp}`)) {
    ok('通用表单新建设备成功(modbus_tcp)');
  } else {
    bad('通用表单新建设备失败');
  }
} else {
  bad('modbus 表单里找不到 source_ip 字段');
}

// ---- 5. 设备管理里编辑一台 scada 设备 ----
await page.goto(BASE + '/devices', { waitUntil: 'networkidle' });
await page.waitForTimeout(1000);
const scadaRow = page.locator('.ant-table-row', { hasText: 'scada' }).first();
if (await scadaRow.count()) {
  await scadaRow.locator('button').first().click();
  await page.waitForTimeout(2000);
  await page.screenshot({ path: `${SHOT}/05-edit-scada.png` });
  modal = await page.locator('.ant-modal').innerText();
  if (/网关/.test(modal)) ok('设备管理编辑 scada 进入专属界面');
  else bad('编辑 scada 未进入专属界面', modal.slice(0, 300).replace(/\n/g, '|'));
  const codeVal = await page.locator('.ant-modal input[placeholder*="N2704"]').first().inputValue().catch(() => '');
  if (codeVal) ok('编辑态已回填测点', codeVal);
  else bad('编辑态测点未回填');
  const proto = page.locator('.ant-modal .ant-select-disabled').first();
  if (await proto.count()) ok('编辑态禁止改协议');
  else bad('编辑态协议仍可改');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(500);
} else {
  bad('设备列表里没有 scada 设备可编辑');
}

// ---- 6. 设备详情页「修改配置」入口 ----
const devs = await (await fetch('http://127.0.0.1:8000/api/config/devices/')).json();
const scadaDev = (devs.results ?? devs).find((d) => d.protocol === 'scada');
await page.goto(`${BASE}/devices/${scadaDev.id}`, { waitUntil: 'networkidle' });
await page.waitForTimeout(1500);
await page.getByRole('button', { name: /修改配置/ }).click();
await page.waitForTimeout(2500);
let m = await page.locator('.ant-modal').innerText().catch(() => '');
if (/网关/.test(m)) ok('设备详情页「修改配置」进入 scada 专属界面');
else bad('详情页修改配置未进入专属界面');

// ---- 7. scada 界面上的两表 Excel 入口 ----
if (/下载 Excel 模板/.test(m) && /导入 Excel/.test(m) && /导出当前网关/.test(m)) {
  ok('scada 配置界面有两表 Excel 的下载/导入/导出入口');
} else {
  bad('scada 配置界面缺 Excel 入口', m.slice(0, 200).replace(/\n/g, '|'));
}
// 真下载一次模板,确认拿到的是两表格式而不是 40 列通用表
const dl = page.waitForEvent('download', { timeout: 15000 });
await page.locator('.ant-modal').getByRole('button', { name: /下载 Excel 模板/ }).click();
try {
  const file = await dl;
  const name = file.suggestedFilename();
  if (/scada/.test(name)) ok('下载到的是 scada 两表模板', name);
  else bad('下载到的模板不对', name);
} catch (e) { bad('模板下载没触发', e.message); }
await page.keyboard.press('Escape');
await page.waitForTimeout(400);

// ---- 8. 设备管理页模板下拉能选到 scada 两表模板 ----
await page.goto(BASE + '/devices', { waitUntil: 'networkidle' });
await page.waitForTimeout(900);
await page.getByRole('button', { name: /下载 Excel 模板/ }).click();
await page.waitForTimeout(600);
const menu = await page.locator('.ant-dropdown-menu').last().innerText();
if (/通用模板/.test(menu) && /两表/.test(menu)) ok('模板下拉同时给出通用模板与 scada 两表模板');
else bad('模板下拉没有按协议分发', menu.replace(/\n/g, '|'));

// ---- 9. 真跑一次 Excel 导入(用导出的文件回灌,验证 round-trip)----
// 先从后端拿一份该网关的导出,等下原样灌回去 —— 导出/导入格式必须自洽。
const gws = await (await fetch('http://127.0.0.1:8000/api/config/scada-gateways/')).json();
const gwId = (gws.results ?? gws)[0].id;
const EXPORTED_XLSX = `${SHOT}/gw_export.xlsx`;
writeFileSync(
  EXPORTED_XLSX,
  Buffer.from(
    await (await fetch(`http://127.0.0.1:8000/api/config/scada-gateways/${gwId}/export/`)).arrayBuffer(),
  ),
);
await page.goto(`${BASE}/devices/${scadaDev.id}`, { waitUntil: 'networkidle' });
await page.waitForTimeout(1200);
await page.getByRole('button', { name: /修改配置/ }).click();
await page.waitForTimeout(2000);
const before = ((await (await fetch('http://127.0.0.1:8000/api/config/devices/')).json()).results ?? []).length;
await page.locator('.ant-modal input[type=file]').setInputFiles(EXPORTED_XLSX);
await page.waitForTimeout(3000);
await page.screenshot({ path: `${SHOT}/07-import.png` });
const bodyAfter = await page.locator('body').innerText();
const modalClosed = (await page.locator('.ant-modal-content').count()) === 0
  || !(await page.locator('.ant-modal-content').first().isVisible().catch(() => false));
const after = ((await (await fetch('http://127.0.0.1:8000/api/config/devices/')).json()).results ?? []).length;
if (modalClosed && /导入完成/.test(bodyAfter)) ok('导出的 Excel 能原样导回(round-trip)');
else bad('Excel 导入失败', bodyAfter.slice(0, 200).replace(/\n/g, '|'));
if (after === before) ok('幂等:重复导入没有多建设备', `${before} → ${after}`);
else bad('重复导入建出了重复设备', `${before} → ${after}`);

console.log('\n=== console errors ===');
console.log(errs.filter((e) => !/favicon|ResizeObserver|findDOMNode|Warning:/.test(e)).slice(0, 15).join('\n') || '(none)');
console.log('\n=== summary ===');
console.log(`${results.filter((r) => r[0] === 'PASS').length} passed, ${results.filter((r) => r[0] === 'FAIL').length} failed`);
await browser.close();
process.exit(results.some((r) => r[0] === 'FAIL') ? 1 : 0);
