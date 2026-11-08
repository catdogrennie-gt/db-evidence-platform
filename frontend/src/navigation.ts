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
