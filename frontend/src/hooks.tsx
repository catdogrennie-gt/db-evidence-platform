import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import type { NavigateOptions } from 'react-router-dom';
import { scopedDestination } from './navigation';
import { api, ApiError, query } from './api';
import type { Actor, Row } from './api';
export const SessionContext = createContext<{ actor: Actor; reload: () => void }>({
  actor: {},
  reload: () => {},
});
export const useSession = () => useContext(SessionContext);
export function useCan() {
  const { actor } = useSession();
  return (...actions: string[]) =>
    actions.some(
      (action) => actor.permissions?.includes(action) || actor.permissions?.includes('*'),
    );
}
export interface Scope {
  department_id?: string;
  system_id?: string;
  environment?: string;
  instance_id?: string;
  from: string;
  to: string;
  timezone: string;
}
export const RESOURCE_REFRESH_MS = 60_000;
export const ScopeContext = createContext<{ scope: Scope; params: string; refresh: number; rolling?: boolean }>({
  scope: { from: '', to: '', timezone: 'Asia/Shanghai' },
  params: '',
  refresh: 0,
});
export const useScope = () => useContext(ScopeContext);
export function useScopedNavigate() {
  const navigate = useNavigate();
  const location = useLocation();
  const { params } = useScope();
  const latest = useRef({ params, search: location.search });
  latest.current = { params, search: location.search };
  return useCallback(
    (to: string, options?: NavigateOptions) =>
      navigate(scopedDestination(to, latest.current.params || latest.current.search), options),
    [navigate],
  );
}
export function useCursor() {
  const { params } = useScope();
  const [cursor, setCursor] = useState<string>();
  useEffect(() => setCursor(undefined), [params]);
  return [cursor, setCursor] as const;
}
interface ResourceState<T> {
    identity: string | null;
    path: string | null;
    data?: T;
    error?: Error;
    loading: boolean;
    refreshing: boolean;
    refreshError?: Error;
    updatedAt?: string;
}
// Retain one authorized resource only. This is not a cache shared between pages or users.
export function useResource<T = Row>(path: string | null, version = 0, options: { rollingWindow?: boolean } = {}) {
  const { actor } = useSession();
  const { rolling } = useScope();
  const authorization = JSON.stringify({
    username: actor.username, user: actor.user, permissions: actor.permissions,
    roles: actor.roles, scope: actor.scope_summary, revision: actor.permission_version,
  });
  const rollingWindow = options.rollingWindow ?? rolling ?? false;
  let requestIdentity = path;
  if (path && rollingWindow) {
    const url = new URL(path, 'http://resource.invalid');
    url.searchParams.delete('from');
    url.searchParams.delete('to');
    url.searchParams.sort();
    requestIdentity = `${url.pathname}?${url.searchParams.toString()}`;
  }
  const identity = path ? `${authorization}\n${requestIdentity}` : null;
  const [state, setState] = useState<ResourceState<T>>({ identity, path, loading: !!path, refreshing: false });
  const [reloadKey, setReloadKey] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    if (!path) {
      setState({ identity, path, loading: false, refreshing: false });
      return;
    }
    setState(previous => previous.identity === identity && previous.data !== undefined
      ? { ...previous, path, loading: false, refreshing: true, error: undefined, refreshError: undefined }
      : { identity, path, loading: true, refreshing: false });
    api
      .get<T>(path, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setState({ identity, path, data, loading: false, refreshing: false, updatedAt: new Date().toISOString() });
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        const failure = error instanceof Error ? error : new Error('请求失败');
        // A denial/nonexistent resource can mean its grant has been revoked. Never retain it.
        const withdraw = error instanceof ApiError && [401, 403, 404].includes(error.status);
        setState(previous => !withdraw && previous.identity === identity && previous.data !== undefined
          ? { ...previous, path, loading: false, refreshing: false, error: undefined, refreshError: failure }
          : { identity, path, error: failure, loading: false, refreshing: false });
      });
    return () => controller.abort();
  }, [path, identity, version, reloadKey]);
  // Gate during render, before the next effect: a changed scope must never flash old data.
  const current = state.identity === identity && path
    ? { ...state, refreshing: state.refreshing || (state.path !== path && state.data !== undefined) }
    : { loading: !!path, refreshing: false, data: undefined, error: undefined, refreshError: undefined, updatedAt: undefined };
  const reload = useCallback(() => setReloadKey((v) => v + 1), []);
  return { ...current, reload };
}
export function errorMessage(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 404) return '资源不可访问，可能已撤销授权或资源不存在。';
    if (error.status === 403) return '当前身份未获得此操作权限。';
    if (error.status === 409) return `状态已变化：${error.message}。请重新加载后核对。`;
    return error.message;
  }
  return error instanceof Error ? error.message : '请求未完成，请稍后重试。';
}
export function useScopedPath(path: string, extra: Row = {}) {
  const { scope } = useScope();
  return `${path}?${query({ ...scope, ...extra })}`;
}
