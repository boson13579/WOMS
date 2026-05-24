import { expect, type Page, test } from '@playwright/test';

import { createTestUser, getEnvUser, loginViaUi, registerUser } from '../helpers/auth';

function userRow(page: Page, username: string) {
  return page.getByRole('row').filter({ hasText: username });
}

test.describe('User management', () => {
  test('root can search registered accounts', async ({ page, request }) => {
    const root = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      root === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to run root user-management tests.',
    );

    const user = createTestUser('user_management_search');
    await registerUser(request, user);

    await loginViaUi(page, root);
    await page.goto('/users');

    await expect(page).toHaveURL('/users');
    await expect(page.getByRole('heading', { name: 'User Management' })).toBeVisible();

    await page.getByLabel('Search users').fill(user.username);

    const row = userRow(page, user.username);
    await expect(row).toBeVisible();
    await expect(row).toContainText(user.email);
  });

  test('root can search accounts by email', async ({ page, request }) => {
    const root = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      root === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to run root user-management tests.',
    );

    const user = createTestUser('user_management_email');
    await registerUser(request, user);

    await loginViaUi(page, root);
    await page.goto('/users');
    await page.getByLabel('Search users').fill(user.email);

    const row = userRow(page, user.username);
    await expect(row).toBeVisible();
    await expect(row).toContainText(user.email);
  });

  test('root can change an account role', async ({ page, request }) => {
    const root = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      root === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to run root user-management tests.',
    );

    const user = createTestUser('user_management_role');
    await registerUser(request, user);

    await loginViaUi(page, root);
    await page.goto('/users');
    await page.getByLabel('Search users').fill(user.username);

    const row = userRow(page, user.username);
    await expect(row).toBeVisible();

    await row.getByRole('button', { name: 'Edit' }).click();
    await row.getByLabel(`Role for ${user.username}`).selectOption('scheduler');
    await row.getByRole('button', { name: 'Save' }).click();

    await expect(row).toContainText('Scheduler');

    await page.getByRole('button', { name: 'Logout' }).click();
    await expect(page).toHaveURL(/\/login/);

    await loginViaUi(page, user);
    await page.getByRole('link', { name: 'Orders' }).click();

    await expect(page.getByTestId('orders-schedule-button')).toBeVisible();
  });

  test('root can deactivate an account and that account cannot login', async ({
    page,
    request,
  }) => {
    const root = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      root === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to run root user-management tests.',
    );

    const user = createTestUser('user_management_deactivate');
    await registerUser(request, user);

    await loginViaUi(page, root);
    await page.goto('/users');
    await page.getByLabel('Search users').fill(user.username);

    const row = userRow(page, user.username);
    await expect(row).toBeVisible();
    await row.getByRole('button', { name: 'Deactivate' }).click();
    await expect(row).toContainText('Inactive');

    await page.getByRole('button', { name: 'Logout' }).click();
    await expect(page).toHaveURL(/\/login/);

    await page.getByLabel('Username').fill(user.username);
    await page.getByLabel('Password').fill(user.password);
    await page.getByRole('button', { name: 'Sign in' }).click();

    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByRole('alert')).toBeVisible();
  });

  test('root cannot deactivate or change their own account', async ({ page }) => {
    const root = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      root === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to run root user-management tests.',
    );

    await loginViaUi(page, root);
    await page.goto('/users');
    await page.getByLabel('Search users').fill(root.username);

    const row = userRow(page, root.username);
    await expect(row).toBeVisible();
    await expect(row.getByRole('button', { name: 'Edit' })).toBeDisabled();
    await expect(row.getByRole('button', { name: 'Deactivate' })).toBeDisabled();
  });

  test('root sees an empty state when no accounts match search', async ({ page }) => {
    const root = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      root === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to run root user-management tests.',
    );

    const suffix = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;

    await loginViaUi(page, root);
    await page.goto('/users');
    await page.getByLabel('Search users').fill(`e2e_no_such_user_${suffix}`);

    await expect(page.getByText('No users found.')).toBeVisible();
  });
});
