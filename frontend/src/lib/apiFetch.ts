/**
 * Shared fetch helper — promoted from `features/dashboard/api/apiFetch.ts`
 * once cross-feature use became necessary.
 *
 * On non-2xx the backend normally returns `{ error: { code, message, details } }`
 * (see `backend/app/api/errors.py`). FastAPI validation/auth errors may use
 * `{ detail }`, so both shapes are supported.
 *
 * 204 No Content short-circuits the parse step and returns `undefined`.
 *
 * AbortError from the timeout is rewritten to a friendlier message so
 * the dashboard's "Failed to load" UI carries useful copy instead of a
 * generic "The user aborted a request.".
 *
 * 401 responses additionally fire a process-wide ``unauthorizedHandler``
 * (registered by the auth store at boot via ``installUnauthorizedHandler``)
 * so any 401 — wherever it surfaces in the UI — can centrally clear the
 * React Query cache, log out locally, and redirect to ``/login?next=…``.
 * The handler is invoked once per 401 and wrapped in try/catch so a
 * misbehaving handler can never mask the underlying ``ApiError``.
 */

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    /**
     * Server-issued correlation id, surfaced via the `X-Request-Id` response
     * header (see backend A4 middleware). Optional because (a) some test mocks
     * don't bother setting the header, and (b) existing call-sites instantiate
     * `new ApiError(status, message)` without it — keeping the field optional
     * preserves source-compat.
     */
    public readonly requestId?: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export function jsonHeaders(): HeadersInit {
  return { 'Content-Type': 'application/json' };
}

let unauthorizedHandler: (() => void) | null = null;

/**
 * Register (or unregister with ``null``) the global 401 callback.
 *
 * Calling this twice replaces the previous handler — the wiring is meant
 * to be installed exactly once at app boot. The handler must be
 * idempotent: a burst of 401s on the same render tick will fire it
 * multiple times, and the implementation in the auth store guards
 * against re-entrancy with the QC cache + navigate side-effects.
 */
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  unauthorizedHandler = fn;
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
      // Capture the server-issued correlation id once. The header is set
      // by the A4 request-id middleware; on 401 the user is being logged
      // out so we don't display it, but populating the field is free and
      // lets non-toast consumers (logging, devtools) still correlate.
      const requestId = res.headers.get('X-Request-Id') ?? undefined;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any, @typescript-eslint/no-unsafe-assignment
      const body = await res.json().catch((): any => ({}));
      const msg: string =
        // eslint-disable-next-line @typescript-eslint/no-unsafe-member-access
        (body?.error?.message as string | undefined) ??
        // eslint-disable-next-line @typescript-eslint/no-unsafe-member-access
        (body?.detail as string | undefined) ??
        res.statusText;
      if (res.status === 401 && unauthorizedHandler) {
        // Wrap in try/catch so a misbehaving handler (e.g. throwing from
        // a stale closure) can never replace the API error with a
        // generic ``Error`` — callers still observe ``ApiError(401)``.
        try {
          unauthorizedHandler();
        } catch {
          // Swallow — see comment above.
        }
      }
      throw new ApiError(res.status, msg, requestId);
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
