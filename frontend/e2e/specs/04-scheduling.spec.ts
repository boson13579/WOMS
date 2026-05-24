import { expect, test } from '@playwright/test';

import { createUserWithRole, getEnvUser, loginViaUi } from '../helpers/auth';
import { uniqueSuffix } from '../helpers/data';
import { createOrderViaUi } from '../helpers/orders';

function addDays(dateText: string, days: number): string {
  const date = new Date(`${dateText}T00:00:00`);
  date.setDate(date.getDate() + days);
  return date.toISOString().slice(0, 10);
}

function todayText(): string {
  return new Date().toISOString().slice(0, 10);
}

test.describe('Scheduling', () => {
  test('scheduler 可以在日曆未排程清單看到 pending 訂單', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const scheduler = await createUserWithRole(request, admin, 'scheduler', 'calendar_pending');

    await loginViaUi(page, scheduler);
    await page.getByRole('link', { name: 'Orders' }).click();
    await createOrderViaUi(page, {
      customerName: `Calendar Pending ${uniqueSuffix()}`,
      waferQuantity: '125',
      requestedDeliveryDate: '2026-06-30',
    });

    await page.getByTestId('orders-calendar-button').click();

    await expect(page.getByTestId('orders-calendar-dialog')).toBeVisible();
    await expect(page.getByTestId('orders-calendar-grid')).toBeVisible();

    const firstUnscheduledOrder = page
      .getByTestId('orders-calendar-unscheduled-list')
      .getByTestId('orders-calendar-unscheduled-order')
      .first();
    await expect(firstUnscheduledOrder).toBeVisible();
    await expect(firstUnscheduledOrder).toContainText(/ORD-\d{8}-\d{4}/);
  });

  test('scheduler 可以在日曆用搜尋縮小未排程訂單清單', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const scheduler = await createUserWithRole(request, admin, 'scheduler', 'calendar_search');

    await loginViaUi(page, scheduler);
    await page.getByRole('link', { name: 'Orders' }).click();

    await page.getByTestId('orders-calendar-button').click();

    const calendarDialog = page.getByTestId('orders-calendar-dialog');
    const unscheduledOrders = calendarDialog
      .getByTestId('orders-calendar-unscheduled-list')
      .getByTestId('orders-calendar-unscheduled-order');
    await expect(unscheduledOrders.first()).toBeVisible();

    const firstOrderText = await unscheduledOrders.first().innerText();
    const orderNumber = firstOrderText.match(/ORD-\d{8}-\d{4}/)?.[0];
    expect(orderNumber).toBeTruthy();

    await calendarDialog.locator('aside button').first().click();
    const calendarSearchInput = calendarDialog.locator('input[type="text"]').first();
    await calendarSearchInput.fill(orderNumber ?? '');

    await expect(unscheduledOrders).toHaveCount(1);
    await expect(unscheduledOrders.first()).toContainText(orderNumber ?? '');

    await calendarSearchInput.fill('no-such-calendar-order');

    await expect(unscheduledOrders).toHaveCount(0);
  });

  test('scheduler 可以切換日曆日期', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const scheduler = await createUserWithRole(request, admin, 'scheduler', 'calendar_dates');
    const firstDate = todayText();
    const secondDate = addDays(firstDate, 1);

    await loginViaUi(page, scheduler);
    await page.getByRole('link', { name: 'Orders' }).click();
    await page.getByTestId('orders-calendar-button').click();

    await expect(page.getByTestId('orders-calendar-dialog')).toBeVisible();

    await page.getByRole('button', { name: new RegExp(`^${secondDate}`) }).click();
    await expect(page.getByRole('button', { name: new RegExp(`^${secondDate}`) })).toBeVisible();

    await page.getByRole('button', { name: new RegExp(`^${firstDate}`) }).click();
    await expect(page.getByRole('button', { name: new RegExp(`^${firstDate}`) })).toBeVisible();
  });

  test('scheduler 可以觸發排程產生', async ({ page, request }) => {
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
