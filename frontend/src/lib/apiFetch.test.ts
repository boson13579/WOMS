import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiFetch, ApiError, setUnauthorizedHandler } from './apiFetch';

describe('apiFetch', () => {
  afterEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
    setUnauthorizedHandler(null);
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

  // RED: setUnauthorizedHandler doesn't exist yet — the import will fail.
  it('invokes the unauthorized handler on 401 and still throws ApiError(401)', async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);

    vi.mocked(global.fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Unauthorized.' }), {
        status: 401,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await expect(
      apiFetch('/api/v1/example', { credentials: 'include' }, (raw) => raw),
    ).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  // RED: 403 and 500 must NOT invoke the unauthorized handler.
  it('does NOT invoke the unauthorized handler on 403/500', async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);

    vi.mocked(global.fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Forbidden.' }), {
        status: 403,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    await expect(
      apiFetch('/api/v1/example', { credentials: 'include' }, (raw) => raw),
    ).rejects.toBeInstanceOf(ApiError);
    expect(handler).not.toHaveBeenCalled();

    vi.mocked(global.fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Boom.' }), {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    await expect(
      apiFetch('/api/v1/example', { credentials: 'include' }, (raw) => raw),
    ).rejects.toBeInstanceOf(ApiError);
    expect(handler).not.toHaveBeenCalled();
  });

  // RED: setUnauthorizedHandler(null) should stop further invocations.
  it('stops invoking the handler once it is unregistered with null', async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    setUnauthorizedHandler(null);

    vi.mocked(global.fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Unauthorized.' }), {
        status: 401,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await expect(
      apiFetch('/api/v1/example', { credentials: 'include' }, (raw) => raw),
    ).rejects.toBeInstanceOf(ApiError);
    expect(handler).not.toHaveBeenCalled();
  });

  // RED: A throwing handler must not mask the API error.
  it('does not mask the API error when the handler throws', async () => {
    setUnauthorizedHandler(() => {
      throw new Error('handler exploded');
    });

    vi.mocked(global.fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Unauthorized.' }), {
        status: 401,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await expect(
      apiFetch('/api/v1/example', { credentials: 'include' }, (raw) => raw),
    ).rejects.toBeInstanceOf(ApiError);
  });

  it('attaches X-Request-Id to ApiError when the header is present', async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Bad.' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json', 'X-Request-Id': 'req-abc' },
      }),
    );

    const caught = await apiFetch('/api/v1/example', { credentials: 'include' }, (raw) => raw)
      .then(() => null)
      .catch((err: unknown) => err);

    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).requestId).toBe('req-abc');
  });

  it('leaves requestId undefined when the X-Request-Id header is absent', async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Bad.' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    const caught = await apiFetch('/api/v1/example', { credentials: 'include' }, (raw) => raw)
      .then(() => null)
      .catch((err: unknown) => err);

    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).requestId).toBeUndefined();
  });

  it('still attaches requestId on 401 responses', async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: 'Unauthorized.' }), {
        status: 401,
        headers: { 'Content-Type': 'application/json', 'X-Request-Id': 'req-401' },
      }),
    );

    const caught = await apiFetch('/api/v1/example', { credentials: 'include' }, (raw) => raw)
      .then(() => null)
      .catch((err: unknown) => err);

    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).requestId).toBe('req-401');
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
