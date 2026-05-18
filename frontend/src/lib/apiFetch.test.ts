import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiFetch } from './apiFetch';

describe('apiFetch', () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it('uses FastAPI detail as the thrown message when the unified envelope is absent', async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Invalid query parameter.' }), {
        status: 422,
        statusText: 'Unprocessable Entity',
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await expect(
      apiFetch('/api/v1/example', { credentials: 'include' }, (raw) => raw),
    ).rejects.toThrow('Invalid query parameter.');
  });

  it('prefers the unified error envelope message when both shapes are present', async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          error: { code: 'bad_request', message: 'Unified message.' },
          detail: 'FastAPI detail.',
        }),
        {
          status: 400,
          statusText: 'Bad Request',
          headers: { 'Content-Type': 'application/json' },
        },
      ),
    );

    await expect(
      apiFetch('/api/v1/example', { credentials: 'include' }, (raw) => raw),
    ).rejects.toThrow('Unified message.');
  });
});
