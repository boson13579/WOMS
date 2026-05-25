import { expect, type Page, test } from '@playwright/test';

import {
  createTestUser,
  createUserWithRole,
  getEnvUser,
  loginViaUi,
  registerUser,
} from '../helpers/auth';

async function expectObservabilitySmoke(page: Page): Promise<void> {
  await expect(page.getByRole('heading', { name: 'Observability' })).toBeVisible();

  await expect(page.getByRole('region', { name: 'Service health' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'RED metrics' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'USE resources' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Top endpoints' })).toBeVisible();

  await expect(page.getByRole('heading', { name: 'Rate', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Error rate', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'P95 latency', exact: true })).toBeVisible();
  await expect(page.getByText('DB connections', { exact: true })).toBeVisible();
  await expect(page.getByText('Redis memory', { exact: true })).toBeVisible();
}

test.describe('Observability', () => {
  test('scheduler 可以看到 observability dashboard 的主要區塊', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create observability users.',
    );

    const scheduler = await createUserWithRole(request, admin, 'scheduler', 'observability_sched');

    await loginViaUi(page, scheduler);
    await page.goto('/observability');

    await expect(page).toHaveURL('/observability');
    await expectObservabilitySmoke(page);
  });

  test('root 可以看到 observability dashboard 的主要區塊', async ({ page }) => {
    const root = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      root === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to run root observability tests.',
    );

    await loginViaUi(page, root);
    await page.goto('/observability');

    await expect(page).toHaveURL('/observability');
    await expectObservabilitySmoke(page);
  });

  test('viewer 進入 observability 會被導回首頁', async ({ page, request }) => {
    const viewer = createTestUser('observability_viewer');
    await registerUser(request, viewer);

    await loginViaUi(page, viewer);
    await page.goto('/observability');

    await expect(page).toHaveURL('/');
  });
});
