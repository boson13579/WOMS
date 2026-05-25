import { expect, type Page, test } from '@playwright/test';

import { createUserWithRole, getEnvUser, loginViaUi } from '../helpers/auth';
import { createUnreadNotificationViaOrderUpdate } from '../helpers/notifications';

function notificationCards(page: Page, orderNumber: string) {
  return page.getByTestId('notification-card').filter({ hasText: orderNumber });
}

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

    const card = notificationCards(page, orderNumber).first();
    await expect(card).toBeVisible();

    const readResponse = page.waitForResponse(
      (response) =>
        response.url().includes('/api/v1/notifications/') &&
        response.url().includes('/read') &&
        response.request().method() === 'PATCH',
    );
    await card.getByTestId('notification-mark-read-button').click();
    const response = await readResponse;
    expect(response.status()).toBe(200);
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
    const orderNumber = await createUnreadNotificationViaOrderUpdate(request, user);

    await loginViaUi(page, user);
    await page.getByRole('link', { name: 'Notifications' }).click();

    const cards = notificationCards(page, orderNumber);
    const card = cards.first();
    await expect(card).toBeVisible();

    const readResponse = page.waitForResponse(
      (response) =>
        response.url().includes('/api/v1/notifications/') &&
        response.url().includes('/read') &&
        response.request().method() === 'PATCH',
    );
    await card.getByTestId('notification-mark-read-button').click();
    const response = await readResponse;
    expect(response.status()).toBe(200);

    await page.getByTestId('notifications-all-tab').click();
    await expect(card).toBeVisible();
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
    const firstOrderNumber = await createUnreadNotificationViaOrderUpdate(request, user);
    const secondOrderNumber = await createUnreadNotificationViaOrderUpdate(request, user);

    await loginViaUi(page, user);
    await page.getByRole('link', { name: 'Notifications' }).click();

    await expect(notificationCards(page, firstOrderNumber).first()).toBeVisible();
    await expect(notificationCards(page, secondOrderNumber).first()).toBeVisible();

    const readAllResponse = page.waitForResponse(
      (response) =>
        response.url().includes('/api/v1/notifications/read-all') &&
        response.request().method() === 'PATCH',
    );
    await page.getByTestId('notifications-mark-all-read-button').click();
    const response = await readAllResponse;
    expect(response.status()).toBe(200);

    await expect(notificationCards(page, firstOrderNumber)).toHaveCount(0);
    await expect(notificationCards(page, secondOrderNumber)).toHaveCount(0);
  });

  test('全部標記為已讀後全部分頁仍會顯示通知', async ({ page, request }) => {
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
    const firstOrderNumber = await createUnreadNotificationViaOrderUpdate(request, user);
    const secondOrderNumber = await createUnreadNotificationViaOrderUpdate(request, user);

    await loginViaUi(page, user);
    await page.getByRole('link', { name: 'Notifications' }).click();

    await expect(notificationCards(page, firstOrderNumber).first()).toBeVisible();
    await expect(notificationCards(page, secondOrderNumber).first()).toBeVisible();

    const readAllResponse = page.waitForResponse(
      (response) =>
        response.url().includes('/api/v1/notifications/read-all') &&
        response.request().method() === 'PATCH',
    );
    await page.getByTestId('notifications-mark-all-read-button').click();
    const response = await readAllResponse;
    expect(response.status()).toBe(200);

    await expect(notificationCards(page, firstOrderNumber)).toHaveCount(0);
    await expect(notificationCards(page, secondOrderNumber)).toHaveCount(0);

    await page.getByTestId('notifications-all-tab').click();
    await expect(notificationCards(page, firstOrderNumber).first()).toBeVisible();
    await expect(notificationCards(page, secondOrderNumber).first()).toBeVisible();
    await expect(
      notificationCards(page, firstOrderNumber).getByTestId('notification-mark-read-button'),
    ).toHaveCount(0);
    await expect(
      notificationCards(page, secondOrderNumber).getByTestId('notification-mark-read-button'),
    ).toHaveCount(0);
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
    const firstOrderNumber = await createUnreadNotificationViaOrderUpdate(request, user);
    const secondOrderNumber = await createUnreadNotificationViaOrderUpdate(request, user);

    await loginViaUi(page, user);
    await expect(page.locator('[aria-label$=" unread notifications"]').first()).toBeVisible();

    await page.getByRole('link', { name: 'Notifications' }).click();
    await expect(notificationCards(page, firstOrderNumber).first()).toBeVisible();
    await expect(notificationCards(page, secondOrderNumber).first()).toBeVisible();

    const readAllResponse = page.waitForResponse(
      (response) =>
        response.url().includes('/api/v1/notifications/read-all') &&
        response.request().method() === 'PATCH',
    );
    await page.getByTestId('notifications-mark-all-read-button').click();
    const response = await readAllResponse;
    expect(response.status()).toBe(200);

    await expect(page.locator('[aria-label$=" unread notifications"]')).toHaveCount(0);
  });
});
