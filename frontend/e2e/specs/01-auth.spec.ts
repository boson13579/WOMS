import { expect, test } from '@playwright/test';

import { createTestUser, loginViaUi, registerUser } from '../helpers/auth';

test.describe('Auth flow', () => {
  test('未登入進入訂單頁會被導回登入頁', async ({ page }) => {
    await page.goto('/orders', { waitUntil: 'domcontentloaded' });

    await expect(page).toHaveURL(/\/login/);
  });

  test('使用正確帳密可以登入並進入儀表板', async ({ page, request }) => {
    const user = createTestUser('login');
    await registerUser(request, user);

    await loginViaUi(page, user);

    await expect(page).toHaveURL('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible();
    await expect(page.getByText(`Welcome, ${user.username}`)).toBeVisible();
  });

  test('密碼錯誤時會顯示錯誤訊息並停留在登入頁', async ({ page, request }) => {
    const user = createTestUser('wrong_password');
    await registerUser(request, user);

    await page.goto('/login');
    await page.getByLabel('Username').fill(user.username);
    await page.getByLabel('Password').fill('WrongPassword123');
    await page.getByRole('button', { name: 'Sign in' }).click();

    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByRole('alert')).toBeVisible();
  });

  test('已登入使用者進入登入頁會被導回儀表板', async ({ page, request }) => {
    const user = createTestUser('skip_login');
    await registerUser(request, user);
    await loginViaUi(page, user);
    await expect(page).toHaveURL('/');

    await page.goto('/login');

    await expect(page).toHaveURL('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible();
  });

  test('使用者可以註冊新帳號並用該帳號登入', async ({ page }) => {
    const user = createTestUser('register');

    await page.goto('/register');
    await page.getByLabel('Username').fill(user.username);
    await page.getByLabel('Email').fill(user.email);
    await page.getByLabel('Password', { exact: true }).fill(user.password);
    await page.getByLabel('Confirm Password').fill(user.password);
    await page.getByRole('button', { name: 'Create account' }).click();

    await expect(page).toHaveURL('/login');
    await expect(page.getByRole('status')).toContainText('Account created');

    await loginViaUi(page, user);

    await expect(page).toHaveURL('/');
    await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible();
    await expect(page.getByText(`Welcome, ${user.username}`)).toBeVisible();
  });
});
