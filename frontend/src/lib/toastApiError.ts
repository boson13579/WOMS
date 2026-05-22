/**
 * Toast helper that surfaces the backend `X-Request-Id` correlation id
 * underneath the error message so support can trace a failure back to a
 * specific server log line.
 *
 * `apiFetch` populates `ApiError.requestId` from the `X-Request-Id`
 * response header (set by the A4 middleware). Non-`ApiError` failures
 * still get a toast — we just don't have a request id to show.
 *
 * Sonner renders `description` as plain text; the `\n` reads as a second
 * visual line in the toast card.
 */
import { toast } from 'sonner';

import { ApiError } from './apiFetch';

export function toastApiError(title: string, err: unknown): void {
  const description = err instanceof Error ? err.message : String(err);
  const requestId = err instanceof ApiError ? err.requestId : undefined;
  toast.error(title, {
    description: requestId ? `${description}\nRequest ID: ${requestId}` : description,
  });
}
