import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const root = join(process.cwd(), 'src');
const read = (name) => readFileSync(join(root, name), 'utf8');

test('App owns one runtime choice and polls account/instance resources', () => {
  const source = read('App.tsx');
  assert.match(source, /runtimeChoice/);
  assert.match(source, /getGpuAccounts|getGpuInstances|apiFetch/);
  assert.match(source, /execution_policy/);
  assert.match(source, /retain_idle_minutes/);
  assert.doesNotMatch(source, /shutdown_after_job/);
  assert.doesNotMatch(source, /idle_grace_minutes/);
});

test('create page delegates lifecycle controls to RuntimeSelection', () => {
  const create = read('pages/CreatePage.tsx');
  const selection = read('components/RuntimeSelection.tsx');
  assert.match(create, /<RuntimeSelection\b/);
  assert.match(create, /value=\{runtimeChoice\}/);
  assert.match(create, /onChange=\{onRuntimeChoiceChange\}/);
  assert.match(selection, /RuntimeLifecycle = 'save' \| 'batch' \| 'smart' \| 'retain'/);
  assert.match(selection, /id="runtime-lifecycle"/);
  assert.match(selection, /id="retain-idle-minutes"/);
  assert.match(selection, /retainIdleMinutes/);
  assert.doesNotMatch(create, /id="batch-mode-select"/);
});

test('settings uses account directory and compute uses instance center', () => {
  const settings = read('pages/SettingsPage.tsx');
  const compute = read('pages/ComputePage.tsx');
  assert.match(settings, /AccountDirectory/);
  assert.match(compute, /InstanceCenter/);
  assert.doesNotMatch(compute, /GoogleOAuthSetup/);
});

test('instance center defaults to active tab without redundant all filter', () => {
  const source = read('components/InstanceCenter.tsx');
  assert.match(source, /type Filter = 'active' \| 'retained' \| 'history';/);
  assert.match(source, /useState<Filter>\('active'\)/);
  assert.doesNotMatch(source, /\['all', '全部'\]/);
});

test('workflow activation surfaces allocation readiness and keeps the run button responsive', () => {
  const source = read('components/WorkflowActivation.tsx');
  assert.match(source, /未勾选「允许分配」/);
  assert.match(source, /allowAllocation/);
  assert.match(source, /上次真实冒烟失败/);
  assert.match(source, /disabled=\{busy \|\| !specsLoaded\}/);
  assert.match(source, /say\(`失败：\$\{message\}`/);
});

test('App wires retention success back into the instance state without duplicates', () => {
  const source = read('App.tsx');
  assert.match(source, /const updatedInstance = await apiFetch<GpuInstance>\(/);
  assert.match(source, /method: 'PATCH'/);
  assert.match(source, /\/retention/);
  assert.match(source, /setInstances\(\(current\) =>/);
  assert.match(source, /findIndex\(\(item\) => item\.instance_id === updatedInstance\.instance_id\)/);
});

test('jobs render machine history and only expose retention action through callback', () => {
  const source = read('pages/JobsPage.tsx');
  assert.match(source, /instance_id/);
  assert.match(source, /account_alias_snapshot/);
  assert.match(source, /onRetain\?:/);
  assert.match(source, /onInstance\?:/);
  assert.match(source, /key=\{jobKey\}/);
});

test('auto retry on capacity switch is wired across CreatePage, JobsPage, App, and gpu-api', () => {
  const createSource = read('pages/CreatePage.tsx');
  const jobsSource = read('pages/JobsPage.tsx');
  const appSource = read('App.tsx');
  const apiSource = read('lib/gpu-api.ts');
  assert.match(createSource, /auto-retry-capacity/);
  assert.match(createSource, /无空闲显卡时自动重试/);
  assert.match(jobsSource, /jobs-auto-retry-capacity/);
  assert.match(jobsSource, /capacity_retry_message/);
  assert.match(jobsSource, /停止重试并取消/);
  assert.match(appSource, /auto_retry_on_capacity/);
  assert.match(appSource, /retryGpuJob\(id, key, \{ auto_retry_on_capacity: autoRetryOnCapacity \}\)/);
  assert.match(apiSource, /auto_retry_on_capacity\?: boolean/);
});
