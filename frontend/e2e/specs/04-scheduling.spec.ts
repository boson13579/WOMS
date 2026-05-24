import { expect, test } from '@playwright/test';

import { createUserWithRole, getEnvUser, loginViaUi } from '../helpers/auth';

test.describe('Scheduling', () => {
  test('scheduler 可以從訂單頁觸發排程器', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const scheduler = await createUserWithRole(request, admin, 'scheduler', 'schedule_trigger');

    await loginViaUi(page, scheduler);
    await page.getByRole('link', { name: 'Orders' }).click();

    const triggerResponse = page.waitForResponse(
      (response) =>
        response.url().includes('/api/v1/schedule/trigger') &&
        response.request().method() === 'POST',
    );

    await page.getByTestId('orders-schedule-button').click();

    const response = await triggerResponse;
    expect(response.status()).toBe(202);
  });
});
