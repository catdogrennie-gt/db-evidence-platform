// No network calls. Failed terminal response fixtures exercise the poll contract.
const assert = require('node:assert/strict'),
  fs = require('node:fs'),
  path = require('node:path'),
  Module = require('node:module');
const req = Module.createRequire(path.resolve(__dirname, '../package.json')),
  ts = req('typescript');
for (const ext of ['.ts', '.tsx'])
  Module._extensions[ext] = (module, name) =>
    module._compile(
      ts.transpileModule(fs.readFileSync(name, 'utf8'), {
        compilerOptions: {
          module: ts.ModuleKind.CommonJS,
          target: ts.ScriptTarget.ES2022,
          jsx: ts.JsxEmit.ReactJSX,
          esModuleInterop: true,
        },
      }).outputText,
      name,
    );
const { awaitTask, connectionTestId, connectionTestPassed, TaskFailure } = req('./src/api.ts');
const id = '11111111-1111-4111-8111-111111111111',
  task = {
    id,
    type: 'connection_test',
    status: 'failed',
    created_at: '2026-01-01T00:00:00Z',
    error_code: 'authentication_failed',
    error_message: '数据库认证失败',
    result: { test_id: id },
  };
(async () => {
  await assert.rejects(awaitTask(task), TaskFailure);
  assert.equal(
    (await awaitTask(task, undefined, undefined, { readConnectionTestFailure: true })).status,
    'failed',
  );
  await assert.rejects(
    awaitTask({ ...task, type: 'investigation' }, undefined, undefined, {
      readConnectionTestFailure: true,
    }),
    TaskFailure,
  );
  await assert.rejects(
    awaitTask({ ...task, result: { test_id: '../../private' } }, undefined, undefined, {
      readConnectionTestFailure: true,
    }),
    TaskFailure,
  );
  assert.equal(connectionTestId({ ...task, status: 'running' }), undefined);
  const good = {
    status: 'succeeded',
    fingerprint: 'a'.repeat(64),
    expires_at: '2099-01-01T00:00:00Z',
  };
  assert(connectionTestPassed(good));
  assert(!connectionTestPassed({ ...good, status: 'failed' }));
  assert(!connectionTestPassed({ ...good, status: 'partial' }));
  assert(!connectionTestPassed({ ...good, fingerprint: '' }));
  assert(!connectionTestPassed({ ...good, expires_at: '2020-01-01T00:00:00Z' }));
  const React = req('react'),
    { renderToStaticMarkup } = req('react-dom/server'),
    { MemoryRouter } = req('react-router-dom'),
    { ConnectionTestView } = req('./src/Presentation.tsx'),
    { TaskProgress } = req('./src/ui.tsx');
  const html = renderToStaticMarkup(
    React.createElement(
      MemoryRouter,
      null,
      React.createElement(
        React.Fragment,
        null,
        React.createElement(ConnectionTestView, {
          data: {
            ...good,
            status: 'failed',
            error_code: 'authentication_failed',
            steps: [
              { step: 'database_identity', status: 'failed', error_code: 'authentication_failed' },
            ],
          },
        }),
        React.createElement(TaskProgress, { task }),
      ),
    ),
  );
  assert(
    html.includes('authentication_failed') &&
      html.includes('只读账号认证未通过') &&
      html.includes('数据库身份'),
  );
  assert(!html.includes('状态已变化'));
  console.log('connection contracts: 7 isolated checks passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

// Zabbix-only product does not re-expose legacy onboarding; polling remains a history contract.
const monitoringSource = fs.readFileSync(path.resolve(__dirname, '../src/Monitoring.tsx'), 'utf8');
assert(!monitoringSource.includes('HostSelection') && !monitoringSource.includes('/selection'));
assert(monitoringSource.includes('/monitoring/groups'));
const { historyPath } = req('./src/navigation.ts');
assert.equal(historyPath('evidence', '../../private'), null);
assert.equal(historyPath('users', id), null);
