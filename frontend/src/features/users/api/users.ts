import { apiFetch, jsonHeaders } from '@/lib/apiFetch';

import {
  userListResponseSchema,
  userResponseSchema,
  userUpdateRequestSchema,
  type UserListResponse,
  type UserResponse,
  type UserUpdateRequest,
} from '../types/user';

export async function listUsers(search?: string): Promise<UserListResponse> {
  const params = new URLSearchParams();
  if (search?.trim()) {
    params.set('search', search.trim());
  }

  const url = `/api/v1/users${params.size ? `?${params.toString()}` : ''}`;
  return apiFetch(url, { headers: jsonHeaders(), credentials: 'include' }, (raw) =>
    userListResponseSchema.parse(raw),
  );
}

export async function updateUser(
  userId: string,
  payload: UserUpdateRequest,
): Promise<UserResponse> {
  const body = userUpdateRequestSchema.parse(payload);
  return apiFetch(
    `/api/v1/users/${userId}`,
    {
      method: 'PATCH',
      headers: jsonHeaders(),
      body: JSON.stringify(body),
      credentials: 'include',
    },
    (raw) => userResponseSchema.parse(raw),
  );
}

export async function deactivateUser(userId: string): Promise<UserResponse> {
  return apiFetch(
    `/api/v1/users/${userId}`,
    {
      method: 'DELETE',
      headers: jsonHeaders(),
      credentials: 'include',
    },
    (raw) => userResponseSchema.parse(raw),
  );
}
