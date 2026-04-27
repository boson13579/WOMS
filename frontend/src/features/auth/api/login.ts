/**
 * Authentication API client — Phase 1 mock.
 *
 * Returns a hardcoded fake token after a small artificial delay so the UI can
 * exercise its loading state. Phase 2 replaces this with a real call to
 * `POST /api/v1/auth/login` validated by `zod`.
 */
import { z } from 'zod';

export const loginRequestSchema = z.object({
  username: z.string().min(1, 'Username is required'),
  password: z.string().min(1, 'Password is required'),
});

export const loginResponseSchema = z.object({
  access_token: z.string(),
  token_type: z.literal('bearer'),
  expires_in: z.number().int().positive(),
});

export type LoginRequest = z.infer<typeof loginRequestSchema>;
export type LoginResponse = z.infer<typeof loginResponseSchema>;

/**
 * Mock implementation. Replace with a real fetch in Phase 2:
 *
 *     const res = await fetch('/api/v1/auth/login', {
 *       method: 'POST',
 *       headers: { 'Content-Type': 'application/json' },
 *       body: JSON.stringify(payload),
 *     });
 *     if (!res.ok) throw new Error('Login failed');
 *     return loginResponseSchema.parse(await res.json());
 */
export async function login(payload: LoginRequest): Promise<LoginResponse> {
  loginRequestSchema.parse(payload);

  // Simulate network latency so loading spinners are visible during dev.
  await new Promise((resolve) => {
    setTimeout(resolve, 400);
  });

  return loginResponseSchema.parse({
    access_token: 'mock-jwt-token-replace-in-phase-2',
    token_type: 'bearer',
    expires_in: 3600,
  });
}
