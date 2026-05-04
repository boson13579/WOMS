/**
 * Domain types for the auth feature.
 *
 * Inferred from zod schemas in `api/auth.ts` so there is a single source of
 * truth. Import from here — never re-declare the same shape manually.
 */
import type { z } from 'zod';

import type {
  loginRequestSchema,
  loginResponseSchema,
  registerRequestSchema,
  registerResponseSchema,
} from '../api/auth';

export type LoginRequest = z.infer<typeof loginRequestSchema>;
export type LoginResponse = z.infer<typeof loginResponseSchema>;
export type RegisterRequest = z.infer<typeof registerRequestSchema>;
export type RegisterResponse = z.infer<typeof registerResponseSchema>;

/** Which form is active on the AuthPage. */
export type AuthMode = 'login' | 'register';
