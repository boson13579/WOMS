import { afterEach, describe, expect, it, vi } from 'vitest';

import { deactivateUser, listUsers, updateUser } from './users';

const USER = {
  id: '00000000-0000-0000-0000-000000000001',
  username: 'alice',
  email: 'alice@example.com',
  role: 'viewer',
  is_active: true,
  version_id: 3,
  created_at: '2026-05-04T00:00:00.000Z',
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('users API', () => {
  it('lists users with cookie credentials and no bearer token', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({ users: [USER], total: 1 }));

    const result = await listUsers();

    expect(result.users).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/users', {
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
    });
  });

  it('trims and encodes search queries', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({ users: [], total: 0 }));

    await listUsers(' alice@example.com ');

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/users?search=alice%40example.com', {
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
    });
  });

  it('updates role and active status with version_id', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      jsonResponse({
        ...USER,
        role: 'scheduler',
        is_active: false,
        version_id: 4,
      }),
    );

    const result = await updateUser(USER.id, {
      role: 'scheduler',
      is_active: false,
      version_id: 3,
    });

    expect(result.role).toBe('scheduler');
    expect(result.is_active).toBe(false);
    expect(fetchMock).toHaveBeenCalledWith(`/api/v1/users/${USER.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role: 'scheduler', is_active: false, version_id: 3 }),
      credentials: 'same-origin',
    });
  });

  it('deactivates a user with DELETE and cookie credentials', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({ ...USER, is_active: false, version_id: 4 }));

    const result = await deactivateUser(USER.id);

    expect(result.is_active).toBe(false);
    expect(fetchMock).toHaveBeenCalledWith(`/api/v1/users/${USER.id}`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
    });
  });

  it('throws structured backend error messages', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      jsonResponse({ error: { message: 'Only root users can manage accounts.' } }, 403),
    );

    await expect(listUsers()).rejects.toThrow('Only root users can manage accounts.');
  });

  it('throws detail fallback messages', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      jsonResponse({ detail: 'Version conflict.' }, 409),
    );

    await expect(updateUser(USER.id, { version_id: 99 })).rejects.toThrow('Version conflict.');
  });

  it('throws default fallback messages for malformed error responses', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(new Response('not json', { status: 500 }));

    await expect(deactivateUser(USER.id)).rejects.toThrow('Failed to deactivate user');
  });

  it('rejects invalid update payloads before sending a request', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch');

    await expect(
      updateUser(USER.id, {
        role: 'invalid-role',
        version_id: 3,
      } as never),
    ).rejects.toThrow();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('rejects malformed server responses', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
      jsonResponse({
        users: [{ ...USER, id: 'not-a-uuid' }],
        total: 1,
      }),
    );

    await expect(listUsers()).rejects.toThrow();
  });
});
