/**
 * Shared fetch helper — promoted from `features/dashboard/api/apiFetch.ts`
 * once cross-feature use became necessary.
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

export function jsonHeaders(): HeadersInit {
  return { 'Content-Type': 'application/json' };
}

const DEFAULT_TIMEOUT_MS = 5_000;

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
  try {
    let res: Response;
    try {
      res = await fetch(url, { ...init, signal: controller.signal });
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        throw new Error(`Request timed out after ${timeoutMs}ms`);
      }
      throw err;
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
    try {
      return parse(await res.json());
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        throw new Error(`Request timed out after ${timeoutMs}ms`);
      }
      throw err;
    }
  } finally {
    clearTimeout(timer);
  }
}
