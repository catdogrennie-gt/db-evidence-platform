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
export const ScopeContext = createContext<{ scope: Scope; params: string; refresh: number }>({
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
export function useResource<T = Row>(path: string | null, version = 0) {
  const [state, setState] = useState<{
    path: string | null;
    data?: T;
    error?: Error;
    loading: boolean;
  }>({ path, loading: !!path });
  const [reloadKey, setReloadKey] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    if (!path) {
      setState({ path, loading: false });
      return;
    }
    setState({ path, loading: true });
    api
      .get<T>(path, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setState({ path, data, loading: false });
      })
      .catch((error) => {
        if (!controller.signal.aborted)
          setState({
            path,
            error: error instanceof Error ? error : new Error('请求失败'),
            loading: false,
          });
      });
    return () => controller.abort();
  }, [path, version, reloadKey]);
  const current: { data?: T; error?: Error; loading: boolean } =
    state.path === path ? state : { loading: !!path };
  return { ...current, reload: () => setReloadKey((v) => v + 1) };
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
