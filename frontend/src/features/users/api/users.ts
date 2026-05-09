import {
  userListResponseSchema,
  userResponseSchema,
  userUpdateRequestSchema,
  type UserListResponse,
  type UserResponse,
  type UserUpdateRequest,
} from '../types/user';

function authHeaders(): HeadersInit {
  return { 'Content-Type': 'application/json' };
}

async function readErrorMessage(response: Response, fallback: string): Promise<string> {
  const errorData = (await response.json().catch(() => null)) as {
    detail?: string;
    error?: { message?: string };
  } | null;

  return errorData?.error?.message ?? errorData?.detail ?? fallback;
}

export async function listUsers(search?: string): Promise<UserListResponse> {
  const params = new URLSearchParams();
  if (search?.trim()) {
    params.set('search', search.trim());
  }

  const url = `/api/v1/users${params.size ? `?${params.toString()}` : ''}`;
  const res = await fetch(url, {
    headers: authHeaders(),
    credentials: 'same-origin',
  });

  if (!res.ok) {
    throw new Error(await readErrorMessage(res, 'Failed to load users'));
  }

  return userListResponseSchema.parse(await res.json());
}

export async function updateUser(
  userId: string,
  payload: UserUpdateRequest,
): Promise<UserResponse> {
  const body = userUpdateRequestSchema.parse(payload);
  const res = await fetch(`/api/v1/users/${userId}`, {
    method: 'PATCH',
    headers: authHeaders(),
    body: JSON.stringify(body),
    credentials: 'same-origin',
  });

  if (!res.ok) {
    throw new Error(await readErrorMessage(res, 'Failed to update user'));
  }

  return userResponseSchema.parse(await res.json());
}

export async function deactivateUser(userId: string): Promise<UserResponse> {
  const res = await fetch(`/api/v1/users/${userId}`, {
    method: 'DELETE',
    headers: authHeaders(),
    credentials: 'same-origin',
  });

  if (!res.ok) {
    throw new Error(await readErrorMessage(res, 'Failed to deactivate user'));
  }

  return userResponseSchema.parse(await res.json());
}
