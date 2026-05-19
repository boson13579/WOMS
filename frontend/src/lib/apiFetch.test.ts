import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiFetch } from './apiFetch';

describe('apiFetch', () => {
  afterEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
  });

  it('parses a 200 response through the provided parser', async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ value: 42 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    const parser = vi.fn((raw: unknown) => (raw as { value: number }).value);

    await expect(apiFetch('/api/v1/example', { credentials: 'include' }, parser)).resolves.toBe(42);
    expect(parser).toHaveBeenCalledWith({ value: 42 });
  });

  it('returns undefined for 204 No Content without calling the parser', async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce(new Response(null, { status: 204 }));
    const parser = vi.fn((raw: unknown) => raw);

    await expect(
      apiFetch('/api/v1/example', { credentials: 'include' }, parser),
    ).resolves.toBeUndefined();
    expect(parser).not.toHaveBeenCalled();
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

  it('rewrites request timeout aborts to a readable error message', async () => {
    vi.useFakeTimers();
    vi.mocked(global.fetch).mockImplementationOnce((_url, init) => {
      const signal = init?.signal;
      return new Promise<Response>((_resolve, reject) => {
        signal?.addEventListener('abort', () => {
          reject(new DOMException('The operation was aborted.', 'AbortError'));
        });
      });
    });

    const promise = apiFetch('/api/v1/slow', { credentials: 'include' }, (raw) => raw, 10);
    const assertion = expect(promise).rejects.toThrow('Request timed out after 10ms');
    await vi.advanceTimersByTimeAsync(10);

    await assertion;
  });
});
