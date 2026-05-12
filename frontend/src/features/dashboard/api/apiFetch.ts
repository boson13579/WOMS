/**
 * Local fetch helper for dashboard API calls.
 *
 * Mirrors the pattern in `features/orders/api/orders.ts`. Duplicated rather
 * than lifted to `lib/` to keep dashboard's API surface self-contained (per
 * Bulletproof React, features should be replaceable without touching other
 * features' code). When a clear cross-feature need arises we can promote
 * this to `src/lib/apiFetch.ts` then.
 */

export function jsonHeaders(): HeadersInit {
  return { 'Content-Type': 'application/json' };
}

/**
 * Default request timeout. The dashboard polls multiple endpoints every
 * 10–30 seconds; a hung request (e.g. backend waiting on a dead Redis
 * before returning 500) would otherwise keep the widget spinning until
 * the OS TCP timeout fires, which can be a minute on Windows. 5s is
 * generous for any of our endpoints and short enough that the React
 * Query error UI shows up before the next polling cycle.
 */
const DEFAULT_TIMEOUT_MS = 5_000;

/**
 * Fetch + parse + unified-error-envelope handling + abort-after-timeout.
 *
 * On non-2xx the backend returns `{ error: { code, message, details } }`
 * (see `backend/app/api/errors.py`). We surface `error.message` as the
 * thrown `Error.message` so React Query toasts / error UIs can render
 * a useful string without parsing the envelope themselves.
 *
 * 204 No Content short-circuits the parse step and returns `undefined`.
 *
 * AbortError from the timeout is rewritten to a friendlier message so
 * the dashboard's "Failed to load" UI carries useful copy instead of a
 * generic "The user aborted a request.".
 */
export async function apiFetch<T>(
  url: string,
  init: RequestInit,
  parse: (raw: unknown) => T,
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => {
    controller.abort();
  }, timeoutMs);
  let res: Response;
  try {
    res = await fetch(url, { ...init, signal: controller.signal });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new Error(`Request timed out after ${timeoutMs}ms`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any, @typescript-eslint/no-unsafe-assignment
    const body = await res.json().catch((): any => ({}));
    const msg: string =
      // eslint-disable-next-line @typescript-eslint/no-unsafe-member-access
      (body?.error?.message as string | undefined) ?? res.statusText;
    throw new Error(msg);
  }
  if (res.status === 204) return undefined as T;
  return parse(await res.json());
}
