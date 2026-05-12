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
 * Fetch + parse + unified-error-envelope handling.
 *
 * On non-2xx the backend returns `{ error: { code, message, details } }`
 * (see `backend/app/api/errors.py`). We surface `error.message` as the
 * thrown `Error.message` so React Query toasts / error UIs can render
 * a useful string without parsing the envelope themselves.
 *
 * 204 No Content short-circuits the parse step and returns `undefined`.
 */
export async function apiFetch<T>(
  url: string,
  init: RequestInit,
  parse: (raw: unknown) => T,
): Promise<T> {
  const res = await fetch(url, init);
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
