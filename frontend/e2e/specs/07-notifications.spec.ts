import { expect, test } from '@playwright/test';

import { createUserWithRole, getEnvUser, loginViaUi } from '../helpers/auth';
import { createUnreadNotificationViaOrderUpdate } from '../helpers/notifications';

test.describe('Notifications', () => {
  test('使用者可以開啟通知中心並切換空的分頁', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create notification test data.',
    );

    const user = await createUserWithRole(request, admin, 'order_manager', 'notifications_empty');

    await loginViaUi(page, user);
    await page.getByRole('link', { name: 'Notifications' }).click();

    await expect(page).toHaveURL('/notifications');
    await expect(page.getByTestId('notifications-empty-state')).toBeVisible();

    await page.getByTestId('notifications-all-tab').click();
    await expect(page.getByTestId('notifications-empty-state')).toBeVisible();

    await page.getByTestId('notifications-unread-tab').click();
    await expect(page.getByTestId('notifications-empty-state')).toBeVisible();
    await expect(page.getByTestId('notifications-mark-all-read-button')).toHaveCount(0);
  });

  test('使用者可以將單一通知標記為已讀', async ({ page, request }) => {
    test.setTimeout(90_000);

    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create notification test data.',
    );

    const user = await createUserWithRole(request, admin, 'order_manager', 'notifications_read');
    const orderNumber = await createUnreadNotificationViaOrderUpdate(request, user);

    await loginViaUi(page, user);
    await page.getByRole('link', { name: 'Notifications' }).click();

    await expect(page.getByTestId('notification-card')).toHaveCount(1);
    await expect(page.getByTestId('notification-card')).toContainText(orderNumber);

    const readResponse = page.waitForResponse(
      (response) =>
        response.url().includes('/api/v1/notifications/') &&
        response.url().includes('/read') &&
        response.request().method() === 'PATCH',
    );
    await page.getByTestId('notification-mark-read-button').click();
    await expect((await readResponse).status()).toBe(200);

    await expect(page.getByTestId('notifications-empty-state')).toBeVisible();
  });

  test('通知已讀後全部分頁仍會顯示通知', async ({ page, request }) => {
    test.setTimeout(90_000);

    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create notification test data.',
    );

    const user = await createUserWithRole(
      request,
      admin,
      'order_manager',
      'notifications_all_tab_read',
    );
    await createUnreadNotificationViaOrderUpdate(request, user);

    await loginViaUi(page, user);
    await page.getByRole('link', { name: 'Notifications' }).click();

    await expect(page.getByTestId('notification-card')).toHaveCount(1);

    const readResponse = page.waitForResponse(
      (response) =>
        response.url().includes('/api/v1/notifications/') &&
        response.url().includes('/read') &&
        response.request().method() === 'PATCH',
    );
    await page.getByTestId('notification-mark-read-button').click();
    await expect((await readResponse).status()).toBe(200);

    await expect(page.getByTestId('notifications-empty-state')).toBeVisible();

    await page.getByTestId('notifications-all-tab').click();
    await expect(page.getByTestId('notification-card')).toHaveCount(1);
    await expect(page.getByTestId('notification-mark-read-button')).toHaveCount(0);
  });

  test('使用者可以將所有通知標記為已讀', async ({ page, request }) => {
    test.setTimeout(120_000);

    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create notification test data.',
    );

    const user = await createUserWithRole(
      request,
      admin,
      'order_manager',
      'notifications_all_read',
    );
    await createUnreadNotificationViaOrderUpdate(request, user);
    await createUnreadNotificationViaOrderUpdate(request, user);

    await loginViaUi(page, user);
    await page.getByRole('link', { name: 'Notifications' }).click();

    await expect(page.getByTestId('notification-card')).toHaveCount(2);

    const readAllResponse = page.waitForResponse(
      (response) =>
        response.url().includes('/api/v1/notifications/read-all') &&
        response.request().method() === 'PATCH',
    );
    await page.getByTestId('notifications-mark-all-read-button').click();
    await expect((await readAllResponse).status()).toBe(200);

    await expect(page.getByTestId('notifications-empty-state')).toBeVisible();
  });

  test('全部標記為已讀後全部分頁仍會顯示通知', async ({
    page,
    request,
  }) => {
    test.setTimeout(120_000);

    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create notification test data.',
    );

    const user = await createUserWithRole(
      request,
      admin,
      'order_manager',
      'notifications_all_tab_all_read',
    );
    await createUnreadNotificationViaOrderUpdate(request, user);
    await createUnreadNotificationViaOrderUpdate(request, user);

    await loginViaUi(page, user);
    await page.getByRole('link', { name: 'Notifications' }).click();

    await expect(page.getByTestId('notification-card')).toHaveCount(2);

    const readAllResponse = page.waitForResponse(
      (response) =>
        response.url().includes('/api/v1/notifications/read-all') &&
        response.request().method() === 'PATCH',
    );
    await page.getByTestId('notifications-mark-all-read-button').click();
    await expect((await readAllResponse).status()).toBe(200);

    await expect(page.getByTestId('notifications-empty-state')).toBeVisible();

    await page.getByTestId('notifications-all-tab').click();
    await expect(page.getByTestId('notification-card')).toHaveCount(2);
    await expect(page.getByTestId('notification-mark-read-button')).toHaveCount(0);
  });

  test('全部標記為已讀後通知徽章數量會清空', async ({ page, request }) => {
    test.setTimeout(120_000);

    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create notification test data.',
    );

    const user = await createUserWithRole(
      request,
      admin,
      'order_manager',
      'notifications_badge_clear',
    );
    await createUnreadNotificationViaOrderUpdate(request, user);
    await createUnreadNotificationViaOrderUpdate(request, user);

    await loginViaUi(page, user);
    await expect(
      page.locator('[aria-label="2 unread notifications"]').filter({ hasText: '2' }),
    ).toBeVisible();

    await page.getByRole('link', { name: 'Notifications' }).click();
    await expect(page.getByTestId('notification-card')).toHaveCount(2);

    const readAllResponse = page.waitForResponse(
      (response) =>
        response.url().includes('/api/v1/notifications/read-all') &&
        response.request().method() === 'PATCH',
    );
    await page.getByTestId('notifications-mark-all-read-button').click();
    await expect((await readAllResponse).status()).toBe(200);

    await expect(page.locator('[aria-label$=" unread notifications"]')).toHaveCount(0);
  });
});
