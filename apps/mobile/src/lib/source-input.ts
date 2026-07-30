export function isAbsoluteServerPath(value: string): boolean {
  const path = value.trim();
  if (!path || path.includes("\0")) return false;
  if (path.startsWith("/")) return true;
  if (/^[A-Za-z]:[\\/]/.test(path)) return true;
  return /^\\\\[^\\]+\\[^\\]+/.test(path) || /^\/\/[^/]+\/[^/]+/.test(path);
}
