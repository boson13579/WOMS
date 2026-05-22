/**
 * Unit tests for the `toastApiError` helper.
 *
 * The helper enriches an error toast with the backend `X-Request-Id`
 * correlation id when the underlying error is an `ApiError` carrying
 * one. Plain `Error` and non-`Error` values fall back gracefully.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from './apiFetch';
// Import after the mock declaration so the helper picks up the mocked
// `toast.error` (the `vi.mock` call is hoisted above this import at
// compile time by Vitest, so the source order here is purely cosmetic).
// eslint-disable-next-line import/order
import { toastApiError } from './toastApiError';

const toastErrorMock = vi.fn();

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: (...args: unknown[]): unknown => toastErrorMock(...args) as unknown,
    loading: vi.fn(),
    info: vi.fn(),
  },
}));

describe('toastApiError', () => {
  beforeEach(() => {
    toastErrorMock.mockReset();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('shows "Request ID: <id>" in the description when ApiError has a requestId', () => {
    toastApiError('Failed', new ApiError(400, 'Bad input.', 'req-abc'));

    expect(toastErrorMock).toHaveBeenCalledTimes(1);
    const [title, opts] = toastErrorMock.mock.calls[0] as [string, { description: string }];
    expect(title).toBe('Failed');
    expect(opts.description).toContain('Bad input.');
    expect(opts.description).toContain('Request ID: req-abc');
  });

  it('omits the Request ID footer when ApiError lacks a requestId', () => {
    toastApiError('Failed', new ApiError(500, 'Server error.'));

    expect(toastErrorMock).toHaveBeenCalledTimes(1);
    const [, opts] = toastErrorMock.mock.calls[0] as [string, { description: string }];
    expect(opts.description).toBe('Server error.');
    expect(opts.description).not.toMatch(/request id/i);
  });

  it('falls back to a stringified description for non-ApiError errors', () => {
    toastApiError('Failed', new Error('Network unreachable.'));
    toastApiError('Failed-string', 'plain string failure');

    expect(toastErrorMock).toHaveBeenCalledTimes(2);
    const [, optsErr] = toastErrorMock.mock.calls[0] as [string, { description: string }];
    expect(optsErr.description).toBe('Network unreachable.');
    expect(optsErr.description).not.toMatch(/request id/i);

    const [titleStr, optsStr] = toastErrorMock.mock.calls[1] as [string, { description: string }];
    expect(titleStr).toBe('Failed-string');
    expect(optsStr.description).toBe('plain string failure');
  });
});
