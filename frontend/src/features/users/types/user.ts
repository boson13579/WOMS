import { z } from 'zod';

export const userRoleSchema = z.enum(['root', 'scheduler', 'order_manager', 'viewer']);
export type UserRole = z.infer<typeof userRoleSchema>;

export const userResponseSchema = z.object({
  id: z.string().uuid(),
  username: z.string(),
  email: z.string().email().nullable(),
  role: userRoleSchema,
  is_active: z.boolean(),
  version_id: z.number().int(),
  created_at: z.string().datetime(),
});

export type UserResponse = z.infer<typeof userResponseSchema>;

export const userListResponseSchema = z.object({
  users: z.array(userResponseSchema),
  total: z.number().int(),
});

export type UserListResponse = z.infer<typeof userListResponseSchema>;

export const userUpdateRequestSchema = z.object({
  username: z.string().min(1).max(64).optional(),
  email: z.string().email().nullable().optional(),
  role: userRoleSchema.optional(),
  is_active: z.boolean().optional(),
  version_id: z.number().int(),
});

export type UserUpdateRequest = z.infer<typeof userUpdateRequestSchema>;
