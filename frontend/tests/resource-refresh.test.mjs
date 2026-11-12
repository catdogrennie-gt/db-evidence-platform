import test, { after, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { createElement, useEffect, useState } from 'react';
import { act, create } from 'react-test-renderer';
import { createServer } from 'vite';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', optimizeDeps: { noDiscovery: true, include: [] } });
after(() => server.close());
const { useResource, SessionContext, RESOURCE_REFRESH_MS } = await server.ssrLoadModule('/src/hooks.tsx');
const { api, ApiError } = await server.ssrLoadModule('/src/api.ts');
const originalGet = api.get;
const originalPost = api.post;
after(() => { api.get = originalGet; api.post = originalPost; });
let calls;
let writes;
beforeEach(() => {
  calls = [];
  writes = [];
  api.get = (path, signal) => new Promise((resolve, reject) => calls.push({ path, signal, resolve, reject }));
  api.post = (...args) => { writes.push(args); return Promise.resolve({}); };
});
const actor = { username: 'reader', permissions: ['data.read'], permission_version: 1, scope_summary: { system_ids: ['orders'] } };
const path = '/intelligence/overview?system_id=orders&from=2026-11-12T01:00:00Z&to=2026-11-12T02:00:00Z';

async function reader(initial = {}) {
  let snapshot;
  let mounts = 0;
  let unmounts = 0;
  const observed = [];
  function ReadingPanel({ value }) {
    const [expanded, setExpanded] = useState(false);
    useEffect(() => { mounts += 1; return () => { unmounts += 1; }; }, []);
    return createElement('details', { open: expanded }, createElement('button', { onClick: () => setExpanded(true) }, value));
  }
  function Reader(props) {
    snapshot = useResource(props.path, props.version ?? 0, { rollingWindow: props.rolling ?? false });
    observed.push({ path: props.path, data: snapshot.data });
    return snapshot.loading ? createElement('span', null, 'loading') : snapshot.data ? createElement(ReadingPanel, { value: snapshot.data.value }) : createElement('span', null, 'empty');
  }
  let props = { path, actor, ...initial };
  const view = () => createElement(SessionContext.Provider, { value: { actor: props.actor, reload() {} } }, createElement(Reader, props));
  let tree;
  await act(async () => { tree = create(view()); });
  return {
    get snapshot() { return snapshot; }, get mounts() { return mounts; }, get unmounts() { return unmounts; }, observed, tree,
    async update(next) { props = { ...props, ...next }; await act(async () => tree.update(view())); },
    async dispose() { await act(async () => tree.unmount()); },
  };
}
async function succeed(index, data = { value: 'first' }) { await act(async () => calls[index].resolve(data)); }
async function fail(index, status) { await act(async () => calls[index].reject(new ApiError(status, 'test_error', 'read failed'))); }

test('same request refresh retains the reading panel and local expansion while marking its old read time', async () => {
  const view = await reader();
  try {
    await succeed(0);
    await act(async () => view.tree.root.findByType('button').props.onClick());
    const first = view.snapshot.data;
    const updatedAt = view.snapshot.updatedAt;
    assert.ok(updatedAt, 'successful reads record an actual timestamp');
    await view.update({ version: 1 });
    assert.equal(view.snapshot.data, first);
    assert.equal(view.snapshot.loading, false);
    assert.equal(view.snapshot.refreshing, true);
    assert.equal(view.snapshot.updatedAt, updatedAt);
    assert.equal(view.mounts, 1);
    assert.equal(view.unmounts, 0);
    assert.equal(view.tree.root.findByType('details').props.open, true);
    await succeed(1, { value: 'second' });
    assert.equal(view.snapshot.data.value, 'second');
    assert.equal(view.snapshot.refreshing, false);
    assert.equal(view.tree.root.findByType('details').props.open, true);
    assert.equal(view.mounts, 1);
  } finally { await view.dispose(); }
});

test('transient refresh failure keeps the last read visibly stale without replacing the reading panel', async () => {
  const view = await reader();
  try {
    await succeed(0);
    const updatedAt = view.snapshot.updatedAt;
    await view.update({ version: 1 });
    await fail(1, 503);
    assert.equal(view.snapshot.data.value, 'first');
    assert.equal(view.snapshot.error, undefined);
    assert.equal(view.snapshot.refreshError.status, 503);
    assert.equal(view.snapshot.updatedAt, updatedAt);
    assert.equal(view.snapshot.refreshing, false);
    assert.equal(view.mounts, 1);
  } finally { await view.dispose(); }
});

for (const status of [401, 403, 404]) test(`HTTP ${status} withdraws previously readable data and does not leave a stale copy`, async () => {
  const view = await reader();
  try {
    await succeed(0);
    await view.update({ version: 1 });
    await fail(1, status);
    assert.equal(view.snapshot.data, undefined);
    assert.equal(view.snapshot.updatedAt, undefined);
    assert.equal(view.snapshot.error.status, status);
    assert.equal(view.snapshot.refreshError, undefined);
    assert.equal(view.unmounts, 1);
  } finally { await view.dispose(); }
});

test('automatic rolling window retains content, but an explicit history window clears it', async () => {
  const view = await reader({ rolling: true });
  try {
    await succeed(0);
    const next = path.replace('01:00:00', '01:01:00').replace('02:00:00', '02:01:00');
    await view.update({ path: next });
    assert.equal(view.snapshot.data.value, 'first');
    assert.equal(view.snapshot.refreshing, true);
    await succeed(1, { value: 'next' });
    const history = path.replace('2026-11-12', '2026-11-11');
    await view.update({ path: history, rolling: false });
    assert.equal(view.snapshot.data, undefined);
    assert.equal(view.snapshot.loading, true);
    assert.ok(view.observed.filter(value => value.path === history).every(value => value.data === undefined));
  } finally { await view.dispose(); }
});

for (const filter of ['system_id', 'department_id', 'instance_id', 'environment', 'timezone', 'itemid']) test(`rolling retention never crosses the ${filter} filter`, async () => {
  const view = await reader({ rolling: true });
  try {
    await succeed(0);
    const next = `${path}&${filter}=another`;
    await view.update({ path: next });
    assert.equal(view.snapshot.data, undefined);
    assert.ok(view.observed.filter(value => value.path === next).every(value => value.data === undefined));
  } finally { await view.dispose(); }
});

test('an authorization revision or user change withdraws all retained data even at the same URL', async () => {
  const view = await reader();
  try {
    await succeed(0);
    await view.update({ actor: { ...actor, permission_version: 2 } });
    assert.equal(view.snapshot.data, undefined);
    assert.equal(calls.length, 2);
    await succeed(1);
    await view.update({ actor: { ...actor, username: 'different-reader' } });
    assert.equal(view.snapshot.data, undefined);
    assert.equal(calls.length, 3);
  } finally { await view.dispose(); }
});

test('leaving a readable resource aborts pending reads and late responses cannot refill another scope', async () => {
  const view = await reader();
  try {
    await succeed(0);
    await view.update({ version: 1 });
    await view.update({ path: null });
    assert.equal(calls[1].signal.aborted, true);
    assert.equal(view.snapshot.data, undefined);
    await succeed(1, { value: 'late unauthorized data' });
    assert.equal(view.snapshot.data, undefined);
  } finally { await view.dispose(); }
});

test('manual refresh only re-reads data; UI polling has a single one-minute interval contract', async () => {
  assert.equal(RESOURCE_REFRESH_MS, 60_000);
  const view = await reader();
  try {
    await succeed(0);
    await act(async () => view.snapshot.reload());
    assert.equal(calls.length, 2);
    assert.equal(calls[1].path, path);
    assert.equal(view.snapshot.refreshing, true);
    assert.deepEqual(writes, []);
  } finally { await view.dispose(); }
});
