/**
 * ``safeNextPath`` — sanitise a ``?next=`` query parameter before handing
 * it to ``navigate(...)``.
 *
 * The global-401 handler and ``AuthOnlyRoute`` both honour ``?next=`` so a
 * bookmarked-deep-link / 401-bounce / login round-trip lands the user where
 * they started. That contract opens a small open-redirect surface: a
 * crafted URL like ``/login?next=//evil.example`` or ``?next=https://attacker``
 * could try to escape the SPA origin.
 *
 * React Router's ``navigate(...)`` treats leading ``//`` as in-app today, so
 * the live exposure is theoretical. This helper tightens the contract
 * explicitly: only same-origin, absolute-path strings ride; anything else
 * collapses to ``/``.
 *
 * Rejected shapes:
 *   - null / empty
 *   - absolute URL (contains ``://``)
 *   - protocol-relative URL (starts with ``//``)
 *   - relative path (does not start with ``/``)
 *
 * The intentional simplicity (string sniffing rather than ``new URL``)
 * keeps the helper safe on inputs that aren't legal URLs at all
 * (``javascript:alert(1)``) — those fall through to the absolute-URL
 * branch via ``://`` detection.
 */
export function safeNextPath(raw: string | null): string {
  if (raw === null || raw === '') return '/';
  if (raw.includes('://')) return '/';
  if (raw.startsWith('//')) return '/';
  if (!raw.startsWith('/')) return '/';
  return raw;
}
