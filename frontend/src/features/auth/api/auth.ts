/**
 * Authentication API client.
 *
 * All responses are validated through zod `.parse()` at the boundary so the
 * rest of the codebase has compile-time type safety.
 */
import { useMutation } from '@tanstack/react-query';
import { z } from 'zod';

export const loginRequestSchema = z.object({
  username: z.string().min(1, 'Username is required'),
  password: z.string().min(8, 'Password must be at least 8 characters'),
});

export const loginResponseSchema = z.object({
  access_token: z.string(),
  token_type: z.literal('bearer'),
});

export const registerRequestSchema = z
  .object({
    username: z
      .string()
      .min(3, 'Username must be at least 3 characters')
      .max(64, 'Username must be at most 64 characters')
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
  email: z.string().email().nullable(),
  role: z.enum(['root', 'scheduler', 'order_manager', 'viewer']),
  is_active: z.boolean(),
  version_id: z.number().int(),
  created_at: z.string().datetime(),
});

export type LoginRequest = z.infer<typeof loginRequestSchema>;
export type LoginResponse = z.infer<typeof loginResponseSchema>;
export type RegisterRequest = z.infer<typeof registerRequestSchema>;
export type RegisterResponse = z.infer<typeof registerResponseSchema>;

async function readErrorMessage(response: Response, fallback: string): Promise<string> {
  const errorData = (await response.json().catch(() => null)) as {
    detail?: string;
    error?: { message?: string };
  } | null;

  return errorData?.error?.message ?? errorData?.detail ?? fallback;
}

export async function login(payload: LoginRequest): Promise<LoginResponse> {
  const body = loginRequestSchema.parse(payload);

  const res = await fetch('/api/v1/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    credentials: 'same-origin',
  });

  if (!res.ok) {
    throw new Error(await readErrorMessage(res, 'Login failed'));
  }

  return loginResponseSchema.parse(await res.json());
}

export async function register(payload: RegisterRequest): Promise<RegisterResponse> {
  const body = registerRequestSchema.parse(payload);

  const res = await fetch('/api/v1/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      username: body.username,
      email: body.email,
      password: body.password,
    }),
    credentials: 'same-origin',
  });

  if (!res.ok) {
    throw new Error(await readErrorMessage(res, 'Registration failed'));
  }

  return registerResponseSchema.parse(await res.json());
}

export function useLogin() {
  return useMutation({ mutationFn: login });
}

export function useRegister() {
  return useMutation({ mutationFn: register });
}

export async function logout(): Promise<void> {
  const res = await fetch('/api/v1/auth/logout', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
  });

  if (!res.ok) {
    throw new Error(await readErrorMessage(res, 'Logout failed'));
  }
}
