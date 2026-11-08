export type Row = Record<string, unknown>;
export interface Page<T = Row> {
  items: T[];
  total: number;
  next_cursor: string | null;
}
export interface Actor extends Row {
  username?: string;
  display_name?: string;
  permissions?: string[];
  roles?: string[];
  csrf_token?: string;
  user?: Row;
  scope_summary?: Row;
  permission_version?: number;
}
export interface TaskRef extends Row {
  id: string;
  type: string;
  status: string;
  created_at: string;
  poll_url?: string;
  result?: Row;
  result_ref?: string;
  error_code?: string;
  error_message?: string;
  progress?: Row;
  needs_external_confirmation?: boolean;
  needs_unknown_cost_acknowledgement?: boolean;
}
export interface Capability extends Row {
  collector: string;
  availability: string;
  reason_code?: string;
  checked_at?: string;
}
export interface Instance extends Row {
  id: string;
  name: string;
  system_id: string;
  environment: string;
  lifecycle: string;
  health?: string;
  data_access?: boolean;
  config_revision: number;
  capabilities?: Capability[];
  last_success_at?: string;
  server_uuid?: string;
  server_version?: string;
}
export interface MetricPoint {
  time: string;
  value: number | null;
  validity: string;
}
export interface MetricSeries extends Row {
  template: string;
  unit: string;
  points: MetricPoint[];
  gaps: unknown[];
  coverage?: Row;
  last_observed_at?: string;
  effective_step_seconds?: number;
}
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details: unknown,
    public requestId?: string,
  ) {
    super(message);
  }
}
export class TaskFailure extends Error {
  constructor(
    public code: string,
    message: string,
    public taskId: string,
  ) {
    super(message);
  }
}
let csrf = '';
const pendingKeys = new Map<string, string>();
const base = '/api/v1';
export function setCsrf(token: string) {
  csrf = token;
}
export function query(values: Record<string, unknown>) {
  const p = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== '')
      for (const v of Array.isArray(value) ? value : [value]) p.append(key, String(v));
  }
  return p.toString();
}
export async function request<T = Row>(
  path: string,
  options: {
    method?: string;
    body?: unknown;
    revision?: number;
    idempotent?: boolean;
    signal?: AbortSignal;
  } = {},
): Promise<T> {
  const method = options.method ?? 'GET';
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  if (method !== 'GET' && csrf) headers['X-CSRF-Token'] = csrf;
  if (options.revision !== undefined) headers['If-Match'] = String(options.revision);
  const requestKey = options.idempotent ? `${path}:${JSON.stringify(options.body)}` : '';
  if (options.idempotent) {
    const key = pendingKeys.get(requestKey) ?? crypto.randomUUID();
    pendingKeys.set(requestKey, key);
    headers['Idempotency-Key'] = key;
  }
  if (!path.startsWith('/') || path.startsWith('//') || /^[a-z]+:/i.test(path))
    throw new Error('无效 API 路径');
  const response = await fetch(path.startsWith(base + '/') ? path : base + path, {
    method,
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    credentials: 'same-origin',
    signal: options.signal,
  });
  const payload =
    response.status === 204 ? undefined : await response.json().catch(() => undefined);
  if (!response.ok) {
    if (response.status < 500 && response.status !== 429) pendingKeys.delete(requestKey);
    const error = payload?.error;
    if (response.status === 401 && !path.startsWith('/auth/'))
      window.dispatchEvent(new Event('session-expired'));
    throw new ApiError(
      response.status,
      error?.code ?? 'http_error',
      error?.message ?? `请求失败 (${response.status})`,
      error?.details,
      error?.request_id,
    );
  }
  if (requestKey) pendingKeys.delete(requestKey);
  return payload as T;
}
export const api = {
  get: <T = Row>(path: string, signal?: AbortSignal) => request<T>(path, { signal }),
  post: <T = Row>(path: string, body: unknown = {}, idempotent = false) =>
    request<T>(path, { method: 'POST', body, idempotent }),
  patch: <T = Row>(path: string, body: unknown, revision?: number) =>
    request<T>(path, { method: 'PATCH', body, revision }),
  put: <T = Row>(path: string, body: unknown) => request<T>(path, { method: 'PUT', body }),
};
export const terminal = (status: string) =>
  ['succeeded', 'partial', 'failed', 'cancelled', 'expired', 'interrupted'].includes(status);
export function connectionTestId(task: TaskRef): string | undefined {
  const id = task.result?.test_id;
  return task.type === 'connection_test' &&
    terminal(task.status) &&
    typeof id === 'string' &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)
    ? id
    : undefined;
}
export function connectionTestPassed(test: Row | undefined, now = Date.now()): boolean {
  return (
    !!test &&
    test.status === 'succeeded' &&
    typeof test.fingerprint === 'string' &&
    /^[0-9a-f]{64}$/i.test(test.fingerprint) &&
    typeof test.expires_at === 'string' &&
    new Date(test.expires_at).valueOf() > now
  );
}
export async function awaitTask(
  ref: TaskRef,
  onProgress?: (task: TaskRef) => void,
  signal?: AbortSignal,
  options?: { readConnectionTestFailure?: boolean },
): Promise<TaskRef> {
  let task = ref;
  for (;;) {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    onProgress?.(task);
    if (terminal(task.status)) {
      if (
        ['failed', 'cancelled', 'expired', 'interrupted'].includes(task.status) &&
        !(options?.readConnectionTestFailure && connectionTestId(task))
      )
        throw new TaskFailure(
          task.error_code ?? task.status,
          task.error_message ?? `任务${task.status}`,
          task.id,
        );
      return task;
    }
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(resolve, 1200);
      signal?.addEventListener(
        'abort',
        () => {
          clearTimeout(timer);
          reject(new DOMException('Aborted', 'AbortError'));
        },
        { once: true },
      );
    });
    task = await api.get<TaskRef>(`/tasks/${encodeURIComponent(ref.id)}`, signal);
  }
}
export function pageItems<T = Row>(data: Page<T> | T[] | undefined): T[] {
  return Array.isArray(data) ? data : (data?.items ?? []);
}
export function text(value: unknown, fallback = '—'): string {
  if (value === null || value === undefined || value === '') return fallback;
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}
export function rows(value: unknown): Row[] {
  return Array.isArray(value) ? value.filter((v): v is Row => !!v && typeof v === 'object') : [];
}
