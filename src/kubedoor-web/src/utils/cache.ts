/**
 * 简单的请求缓存工具
 */
const cache = new Map<string, { data: any; timestamp: number }>();

export function getCached<T>(
  key: string,
  maxAge: number = 5 * 60 * 1000
): T | null {
  const item = cache.get(key);
  if (item && Date.now() - item.timestamp < maxAge) {
    return item.data as T;
  }
  cache.delete(key);
  return null;
}

export function setCache(key: string, data: any): void {
  cache.set(key, { data, timestamp: Date.now() });
}

export function clearCache(keyPrefix?: string): void {
  if (keyPrefix) {
    for (const key of cache.keys()) {
      if (key.startsWith(keyPrefix)) cache.delete(key);
    }
  } else {
    cache.clear();
  }
}
