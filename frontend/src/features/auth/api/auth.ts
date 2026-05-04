/**
 * Authentication API client.
 *
 * Phase 1: mock implementations that simulate network latency.
 * Phase 2: replace mock bodies with real fetch calls to:
 *   POST /api/v1/auth/login
 *   POST /api/v1/auth/register
 *
 * All responses are validated through zod `.parse()` at the boundary so the
 * rest of the codebase has compile-time type safety.
 */
import { useMutation } from '@tanstack/react-query';
import { z } from 'zod';

// ─── Schemas ────────────────────────────────────────────────────────────────

export const loginRequestSchema = z.object({
  username: z.string().min(1, 'Username is required'),
  password: z.string().min(8, 'Password must be at least 8 characters'),
});

export const loginResponseSchema = z.object({
  access_token: z.string(),
  token_type: z.literal('bearer'),
  expires_in: z.number().int().positive(),
});

export const registerRequestSchema = z
  .object({
    username: z
      .string()
      .min(3, 'Username must be at least 3 characters')
      .max(50, 'Username must be at most 50 characters')
      .regex(/^[a-zA-Z0-9_]+$/, 'Username may only contain letters, numbers and underscores'),
    email: z.string().email('Invalid email address'),
    password: z
      .string()
      .min(8, 'Password must be at least 8 characters')
      .regex(/[A-Z]/, 'Password must contain at least one uppercase letter')
      .regex(/[0-9]/, 'Password must contain at least one number'),
    confirmPassword: z.string(),
  })
  .refine((data) => data.password === data.confirmPassword, {
    message: 'Passwords do not match',
    path: ['confirmPassword'],
  });

export const registerResponseSchema = z.object({
  id: z.string().uuid(),
  username: z.string(),
  email: z.string().email(),
  created_at: z.string().datetime(),
});

// ─── Types (re-exported for convenience) ────────────────────────────────────

export type LoginRequest = z.infer<typeof loginRequestSchema>;
export type LoginResponse = z.infer<typeof loginResponseSchema>;
export type RegisterRequest = z.infer<typeof registerRequestSchema>;
export type RegisterResponse = z.infer<typeof registerResponseSchema>;

// ─── API functions ───────────────────────────────────────────────────────────

/**
 * Phase 1 mock — returns a hardcoded token after an artificial delay.
 *
 * Phase 2 replacement:
 *   const res = await fetch('/api/v1/auth/login', {
 *     method: 'POST',
 *     headers: { 'Content-Type': 'application/json' },
 *     body: JSON.stringify(payload),
 *   });
 *   if (!res.ok) throw new Error('Login failed');
 *   return loginResponseSchema.parse(await res.json());
 */
export async function login(payload: LoginRequest): Promise<LoginResponse> {
  loginRequestSchema.parse(payload);

  await new Promise<void>((resolve) => {
    setTimeout(resolve, 400);
  });

  return loginResponseSchema.parse({
    access_token: 'mock-jwt-token-replace-in-phase-2',
    token_type: 'bearer',
    expires_in: 3600,
  });
}

/**
 * Phase 1 mock — simulates user registration.
 *
 * Phase 2 replacement:
 *   const res = await fetch('/api/v1/auth/register', {
 *     method: 'POST',
 *     headers: { 'Content-Type': 'application/json' },
 *     body: JSON.stringify(payload),
 *   });
 *   if (!res.ok) throw new Error('Registration failed');
 *   return registerResponseSchema.parse(await res.json());
 */
export async function register(payload: RegisterRequest): Promise<RegisterResponse> {
  registerRequestSchema.parse(payload);

  await new Promise<void>((resolve) => {
    setTimeout(resolve, 600);
  });

  return registerResponseSchema.parse({
    id: '00000000-0000-0000-0000-000000000001',
    username: payload.username,
    email: payload.email,
    created_at: new Date().toISOString(),
  });
}

// ─── React Query hooks ───────────────────────────────────────────────────────

export function useLogin() {
  return useMutation({ mutationFn: login });
}

export function useRegister() {
  return useMutation({ mutationFn: register });
}
