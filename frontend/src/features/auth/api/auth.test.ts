/**
 * [TDD - RED → GREEN → REFACTOR]
 *
 * Tests for the auth API schemas (zod validation).
 * These are pure unit tests — no DOM rendering needed.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/lib/apiFetch';

import {
  getMe,
  login,
  loginRequestSchema,
  loginResponseSchema,
  logout,
  meResponseSchema,
  register,
  registerRequestSchema,
  registerResponseSchema,
} from './auth';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('loginRequestSchema', () => {
  it('accepts valid credentials', () => {
    const result = loginRequestSchema.safeParse({ username: 'alice', password: 'Password1' });
    expect(result.success).toBe(true);
  });

  it('rejects empty username', () => {
    const result = loginRequestSchema.safeParse({ username: '', password: 'Password1' });
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.issues[0].path).toContain('username');
    }
  });

  it('rejects password shorter than 8 chars', () => {
    const result = loginRequestSchema.safeParse({ username: 'alice', password: 'short' });
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.issues[0].path).toContain('password');
    }
  });
});

describe('loginResponseSchema', () => {
  it('accepts a valid token response', () => {
    const result = loginResponseSchema.safeParse({
      access_token: 'some-jwt',
      token_type: 'bearer',
    });
    expect(result.success).toBe(true);
  });

  it('rejects non-bearer token_type', () => {
    const result = loginResponseSchema.safeParse({
      access_token: 'some-jwt',
      token_type: 'basic',
    });
    expect(result.success).toBe(false);
  });
});

describe('registerRequestSchema', () => {
  const validPayload = {
    username: 'alice123',
    email: 'alice@example.com',
    password: 'Password1',
    confirmPassword: 'Password1',
  };

  it('accepts a valid registration payload', () => {
    const result = registerRequestSchema.safeParse(validPayload);
    expect(result.success).toBe(true);
  });

  it('rejects username shorter than 3 chars', () => {
    const result = registerRequestSchema.safeParse({ ...validPayload, username: 'ab' });
    expect(result.success).toBe(false);
  });

  it('rejects username with special characters', () => {
    const result = registerRequestSchema.safeParse({ ...validPayload, username: 'alice!' });
    expect(result.success).toBe(false);
  });

  it('rejects invalid email format', () => {
    const result = registerRequestSchema.safeParse({ ...validPayload, email: 'not-email' });
    expect(result.success).toBe(false);
  });

  it('rejects password without uppercase', () => {
    const result = registerRequestSchema.safeParse({
      ...validPayload,
      password: 'password1',
      confirmPassword: 'password1',
    });
    expect(result.success).toBe(false);
  });

  it('rejects password without number', () => {
    const result = registerRequestSchema.safeParse({
      ...validPayload,
      password: 'PasswordOnly',
      confirmPassword: 'PasswordOnly',
    });
    expect(result.success).toBe(false);
  });

  it('rejects mismatched passwords', () => {
    const result = registerRequestSchema.safeParse({
      ...validPayload,
      confirmPassword: 'DifferentPass1',
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      const paths = result.error.issues.map((i) => i.path.join('.'));
      expect(paths).toContain('confirmPassword');
    }
  });
});

describe('auth API error handling', () => {
  it('throws backend login error messages', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      jsonResponse({ error: { message: 'Invalid credentials.' } }, 401),
    );

    await expect(login({ username: 'alice', password: 'Password1' })).rejects.toThrow(
      'Invalid credentials.',
    );
  });

  it('throws backend register error messages', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      jsonResponse({ error: { message: "Username 'alice' is already taken." } }, 409),
    );

    await expect(
      register({
        username: 'alice',
        email: 'alice@example.com',
        password: 'Password1',
        confirmPassword: 'Password1',
      }),
    ).rejects.toThrow("Username 'alice' is already taken.");
  });

  it('throws backend logout error messages', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      jsonResponse({ error: { message: 'Logout failed on server.' } }, 500),
    );

    await expect(logout()).rejects.toThrow('Logout failed on server.');
  });
});

// RED: login/register/logout should send credentials: 'include' so the
// httpOnly cookie is attached to the request even when the SPA is served
// from a different origin (e.g. behind a reverse proxy in production).
describe('auth API credential mode', () => {
  it('login sends credentials: include', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      jsonResponse({
        access_token: 'tok',
        token_type: 'bearer',
      }),
    );

    await login({ username: 'alice', password: 'Password1' });

    const init = fetchMock.mock.calls[0]?.[1];
    expect(init?.credentials).toBe('include');
  });

  it('register sends credentials: include', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      jsonResponse({
        id: '00000000-0000-0000-0000-000000000001',
        username: 'alice',
        email: 'alice@example.com',
        role: 'viewer',
        is_active: true,
        version_id: 1,
        created_at: '2026-05-04T00:00:00.000Z',
      }),
    );

    await register({
      username: 'alice123',
      email: 'alice@example.com',
      password: 'Password1',
      confirmPassword: 'Password1',
    });

    const init = fetchMock.mock.calls[0]?.[1];
    expect(init?.credentials).toBe('include');
  });

  it('logout sends credentials: include', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(null, { status: 204 }));

    await logout();

    const init = fetchMock.mock.calls[0]?.[1];
    expect(init?.credentials).toBe('include');
  });
});

// RED: getMe() and meResponseSchema do not exist yet.
describe('getMe', () => {
  const VALID_ME = {
    id: '00000000-0000-0000-0000-000000000002',
    username: 'alice',
    email: 'alice@example.com',
    role: 'viewer',
    is_active: true,
    version_id: 4,
  };

  it('meResponseSchema accepts a valid /auth/me payload', () => {
    const result = meResponseSchema.safeParse(VALID_ME);
    expect(result.success).toBe(true);
  });

  it('returns the parsed user on 200 with credentials: include', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(jsonResponse(VALID_ME));

    const user = await getMe();

    expect(user.id).toBe(VALID_ME.id);
    expect(user.role).toBe('viewer');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/auth/me',
      expect.objectContaining({ credentials: 'include' }),
    );
  });

  it('throws ApiError(401) when the cookie is missing or expired', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      jsonResponse({ detail: 'Not authenticated.' }, 401),
    );

    let caught: unknown;
    try {
      await getMe();
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).status).toBe(401);
  });
});

describe('registerResponseSchema', () => {
  it('accepts a valid server response', () => {
    const result = registerResponseSchema.safeParse({
      id: '00000000-0000-0000-0000-000000000001',
      username: 'alice123',
      email: 'alice@example.com',
      role: 'viewer',
      is_active: true,
      version_id: 1,
      created_at: '2026-05-04T00:00:00.000Z',
    });
    expect(result.success).toBe(true);
  });

  it('rejects a non-UUID id', () => {
    const result = registerResponseSchema.safeParse({
      id: 'not-a-uuid',
      username: 'alice123',
      email: 'alice@example.com',
      role: 'viewer',
      is_active: true,
      version_id: 1,
      created_at: '2026-05-04T00:00:00.000Z',
    });
    expect(result.success).toBe(false);
  });
});
