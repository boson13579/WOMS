/**
 * Tests for ``useAuthStore`` covering:
 *   - JWTs without ``exp`` fall back to ``Date.now() + JWT_DEFAULT_TTL_MS``
 *     (matches backend ``JWT_ACCESS_TOKEN_TTL_SECONDS = 3600``)
 *   - explicit ``exp`` is honoured (no override)
 *   - corrupt JWT segment also falls back to the default TTL, not 0
 *   - ``installUnauthorizedHandler`` wires ``setUnauthorizedHandler`` so a
 *     401 from anywhere clears the QC cache, logs out locally, and
 *     navigates to ``/login?next=…``
 */
import { QueryClient } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { setUnauthorizedHandler } from '@/lib/apiFetch';

import { installUnauthorizedHandler, JWT_DEFAULT_TTL_MS, useAuthStore } from './authStore';

function encodeBase64Url(obj: object): string {
  const json = JSON.stringify(obj);
  // jsdom has ``btoa``; produce base64url to match the JWT spec.
  return btoa(json).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
}

function makeFakeJwt(payload: object): string {
  // signature segment is irrelevant — the store only looks at the payload.
  const header = encodeBase64Url({ alg: 'none', typ: 'JWT' });
  const body = encodeBase64Url(payload);
  return `${header}.${body}.sig`;
}

beforeEach(() => {
  if (typeof window !== 'undefined') {
    window.localStorage.clear();
  }
  useAuthStore.setState({ user: null, expiresAt: null });
  setUnauthorizedHandler(null);
});

afterEach(() => {
  vi.restoreAllMocks();
  setUnauthorizedHandler(null);
});

describe('useAuthStore.setSession', () => {
  // RED: store currently uses ``0`` when ``exp`` is missing.
  it('falls back to Date.now() + JWT_DEFAULT_TTL_MS when the JWT lacks exp', () => {
    const before = Date.now();
    const token = makeFakeJwt({ sub: 'u-1', role: 'viewer' });

    useAuthStore.getState().setSession(token, 'alice');

    const after = Date.now();
    const { expiresAt } = useAuthStore.getState();
    if (typeof expiresAt !== 'number') {
      throw new Error('expected expiresAt to be set');
    }
    expect(expiresAt).toBeGreaterThanOrEqual(before + JWT_DEFAULT_TTL_MS);
    expect(expiresAt).toBeLessThanOrEqual(after + JWT_DEFAULT_TTL_MS);
  });

  it('honours an explicit exp claim', () => {
    const expSeconds = Math.floor(Date.now() / 1000) + 7200; // +2h
    const token = makeFakeJwt({ sub: 'u-1', role: 'scheduler', exp: expSeconds });

    useAuthStore.getState().setSession(token, 'alice');

    expect(useAuthStore.getState().expiresAt).toBe(expSeconds * 1000);
  });

  // RED: catch branch currently falls back to ``expiresAt: 0``.
  it('falls back to the default TTL when the JWT payload is unparseable', () => {
    const before = Date.now();
    // Token with garbled middle segment.
    const token = `${btoa('{"alg":"none"}')}.@@@not-base64@@@.sig`;

    useAuthStore.getState().setSession(token, 'alice');

    const after = Date.now();
    const { expiresAt } = useAuthStore.getState();
    if (typeof expiresAt !== 'number') {
      throw new Error('expected expiresAt to be set');
    }
    expect(expiresAt).toBeGreaterThanOrEqual(before + JWT_DEFAULT_TTL_MS);
    expect(expiresAt).toBeLessThanOrEqual(after + JWT_DEFAULT_TTL_MS);
  });
});

describe('installUnauthorizedHandler', () => {
  // RED: installUnauthorizedHandler does not exist yet.
  it('clears the QueryClient cache, logs out, and navigates to /login?next=…', async () => {
    const qc = new QueryClient();
    qc.setQueryData(['orders', 'snapshot'], { pending: 5 });
    qc.setQueryData(['auth', 'me'], { id: 'me' });

    // Seed an authed session so logout() has something to clear.
    const token = makeFakeJwt({
      sub: 'u-1',
      role: 'viewer',
      exp: Math.floor(Date.now() / 1000) + 60,
    });
    useAuthStore.getState().setSession(token, 'alice');
    expect(useAuthStore.getState().user).not.toBeNull();

    // Pretend we're on /orders so the next param should be /orders.
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { pathname: '/orders', search: '', hash: '' } as Location,
    });

    // Stub the logout HTTP call so the test doesn't depend on global fetch.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));

    const navigate = vi.fn();
    installUnauthorizedHandler(qc, navigate);

    // Simulate a 401 from anywhere by invoking the registered handler.
    // The handler runs async (logout is async) — await the queue.
    const apiFetchModule = await import('@/lib/apiFetch');
    // Force a fake 401 through apiFetch to trigger the handler path.
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Not authenticated.' }), { status: 401 }),
    );

    await expect(
      apiFetchModule.apiFetch('/api/v1/anywhere', { credentials: 'include' }, (raw) => raw),
    ).rejects.toBeInstanceOf(apiFetchModule.ApiError);

    // Allow the handler's queued logout() to settle.
    await new Promise((r) => {
      setTimeout(r, 0);
    });

    expect(qc.getQueryData(['orders', 'snapshot'])).toBeUndefined();
    expect(qc.getQueryData(['auth', 'me'])).toBeUndefined();
    expect(useAuthStore.getState().user).toBeNull();
    expect(navigate).toHaveBeenCalledWith(
      '/login?next=%2Forders',
      expect.objectContaining({ replace: true }),
    );
  });

  it('encodes the current pathname + search in the next param', async () => {
    const qc = new QueryClient();
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { pathname: '/users', search: '?page=2', hash: '' } as Location,
    });
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));

    const navigate = vi.fn();
    installUnauthorizedHandler(qc, navigate);

    const apiFetchModule = await import('@/lib/apiFetch');
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Not authenticated.' }), { status: 401 }),
    );

    await expect(
      apiFetchModule.apiFetch('/api/v1/anywhere', { credentials: 'include' }, (raw) => raw),
    ).rejects.toBeInstanceOf(apiFetchModule.ApiError);

    await new Promise((r) => {
      setTimeout(r, 0);
    });

    expect(navigate).toHaveBeenCalledWith(
      '/login?next=%2Fusers%3Fpage%3D2',
      expect.objectContaining({ replace: true }),
    );
  });
});
