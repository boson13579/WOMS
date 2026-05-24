import { expect, type APIRequestContext, type Page } from '@playwright/test';

import { uniqueSuffix } from './data';

export interface LoginUser {
  username: string;
  password: string;
}

export interface TestUser extends LoginUser {
  email: string;
}

export type UserRole = 'root' | 'scheduler' | 'order_manager' | 'viewer';

export function createTestUser(prefix: string): TestUser {
  const suffix = uniqueSuffix();

  return {
    username: `e2e_${prefix}_${suffix}`,
    email: `e2e_${prefix}_${suffix}@example.com`,
    password: 'Password123',
  };
}

export function getEnvUser(
  usernameEnv: string,
  passwordEnv: string,
  defaultUsername?: string,
): LoginUser | null {
  const username = process.env[usernameEnv] ?? defaultUsername;
  const password = process.env[passwordEnv];

  if (!username || !password) return null;

  return { username, password };
}

export async function registerUser(request: APIRequestContext, user: TestUser): Promise<void> {
  const response = await request.post('/api/v1/auth/register', {
    data: {
      username: user.username,
      email: user.email,
      password: user.password,
    },
  });

  if (!response.ok()) {
    throw new Error(`Failed to register test user: ${response.status()} ${await response.text()}`);
  }
}

export async function loginViaApi(request: APIRequestContext, user: LoginUser): Promise<void> {
  const response = await request.post('/api/v1/auth/login', {
    data: {
      username: user.username,
      password: user.password,
    },
  });

  if (!response.ok()) {
    throw new Error(`Failed to login test user: ${response.status()} ${await response.text()}`);
  }
}

interface UserResponse {
  id: string;
  username: string;
  role: UserRole;
  version_id: number;
}

interface UserListResponse {
  users: UserResponse[];
}

export async function createUserWithRole(
  request: APIRequestContext,
  admin: LoginUser,
  role: Exclude<UserRole, 'viewer'>,
  prefix: string,
): Promise<TestUser> {
  const user = createTestUser(prefix);
  await registerUser(request, user);

  await loginViaApi(request, admin);

  const listResponse = await request.get('/api/v1/users', {
    params: { search: user.username },
  });
  if (!listResponse.ok()) {
    throw new Error(`Failed to list users: ${listResponse.status()} ${await listResponse.text()}`);
  }

  const users = (await listResponse.json()) as UserListResponse;
  const created = users.users.find((candidate) => candidate.username === user.username);
  if (!created) {
    throw new Error(`Could not find newly registered user ${user.username}`);
  }

  const updateResponse = await request.patch(`/api/v1/users/${created.id}`, {
    data: {
      role,
      version_id: created.version_id,
    },
  });
  if (!updateResponse.ok()) {
    throw new Error(
      `Failed to promote ${user.username}: ${updateResponse.status()} ${await updateResponse.text()}`,
    );
  }

  return user;
}

export async function loginViaUi(page: Page, user: LoginUser): Promise<void> {
  await page.goto('/login');
  await page.getByLabel('Username').fill(user.username);
  await page.getByLabel('Password').fill(user.password);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page).not.toHaveURL(/\/login/);
}
