/** Preserve the latest scope; explicit destination parameters take precedence. */
export function scopedDestination(to: string, context: string): string {
  const [route, fragment] = to.split('#', 2);
  const separator = route.indexOf('?');
  const path = separator < 0 ? route : route.slice(0, separator);
  const target = new URLSearchParams(separator < 0 ? '' : route.slice(separator + 1));
  for (const [key, value] of new URLSearchParams(context))
    if (!target.has(key)) target.set(key, value);
  const search = target.toString();
  return path + (search ? '?' + search : '') + (fragment ? '#' + fragment : '');
}

export function historyPath(kind: string, id: string | undefined): string | null {
  if (!['investigations', 'evidence', 'events', 'tasks', 'verifications', 'knowledge'].includes(kind) || !id || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) return null;
  return `/${kind}/${id}`;
}
