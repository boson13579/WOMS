/**
 * Audit-log domain types and Zod schemas.
 *
 * Mirrors the backend `AuditLogResponse` / `AuditLogListResponse`
 * (see `backend/app/schemas/audit.py`) so runtime validation catches
 * shape drift. `user_id` is nullable because system-driven actions
 * (e.g. scheduler-emitted `order.scheduled`) have no human actor;
 * the UI renders those as `(system)`.
 */
import { z } from 'zod';

export const auditEventSchema = z.object({
  id: z.string().uuid(),
  action: z.string(),
  user_id: z.string().uuid().nullable(),
  resource_id: z.string().uuid(),
  old_value: z.record(z.unknown()).nullable(),
  new_value: z.record(z.unknown()).nullable(),
  created_at: z.string(),
});

export type AuditEvent = z.infer<typeof auditEventSchema>;

export const auditEventListResponseSchema = z.object({
  items: z.array(auditEventSchema),
  total: z.number().int(),
  page: z.number().int(),
  page_size: z.number().int(),
});

export type AuditEventListResponse = z.infer<typeof auditEventListResponseSchema>;

export const auditResourceTypeSchema = z.enum(['user', 'order', 'schedule', 'other']);
export type AuditResourceType = z.infer<typeof auditResourceTypeSchema>;

/**
 * Filter shape consumed by the `useAuditEvents` hook and the
 * `AuditFilters` component. All fields optional; an undefined field
 * means "no constraint applied". Empty strings (from cleared `<input>`s)
 * are normalised to `undefined` before being threaded into the query
 * string so the URL doesn't accumulate `?action=&from=` clutter.
 *
 * Each field includes `| undefined` explicitly so callers can spread a
 * partial update onto an existing filter without falling foul of
 * tsconfig's `exactOptionalPropertyTypes: true`.
 */
export interface AuditFiltersState {
  actorId?: string | undefined;
  action?: string | undefined;
  resourceType?: AuditResourceType | undefined;
  /** ISO date `YYYY-MM-DD` — converted to `<from>T00:00:00Z` at fetch time. */
  fromDate?: string | undefined;
  /** ISO date `YYYY-MM-DD` — converted to `<to>T23:59:59Z` at fetch time. */
  toDate?: string | undefined;
}

// The legacy ``KNOWN_AUDIT_ACTIONS`` hard-coded list was removed when the
// Action filter became a dynamic typeahead backed by ``GET /audit/actions``
// (see `features/audit/api/useAuditActions.ts`). The frontend no longer
// owns the canonical action vocabulary — the DB does.
