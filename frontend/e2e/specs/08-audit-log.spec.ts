import { expect, type Page, test } from '@playwright/test';

import {
  createTestUser,
  createUserWithRole,
  getEnvUser,
  loginViaUi,
  registerUser,
} from '../helpers/auth';
import { uniqueSuffix } from '../helpers/data';
import { createOrderViaUi, orderRow } from '../helpers/orders';

function userRow(page: Page, username: string) {
  return page.getByRole('row').filter({ hasText: username });
}

function addDays(dateText: string, days: number): string {
  const date = new Date(`${dateText}T00:00:00`);
  date.setDate(date.getDate() + days);
  return date.toISOString().slice(0, 10);
}

function todayText(): string {
  return new Date().toISOString().slice(0, 10);
}

test.describe('Audit log', () => {
  test('root 可以用 actor action resource type 和日期找到使用者角色變更紀錄', async ({
    page,
    request,
  }) => {
    const root = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      root === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to run audit log tests.',
    );

    const user = createTestUser('audit_role_change');
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

    await page.goto('/audit');
    await expect(page.getByRole('heading', { name: 'Audit Log' })).toBeVisible();

    const actorFilter = page.getByRole('combobox', { name: 'Actor filter' });
    await expect(actorFilter).toBeVisible();
    await actorFilter.fill(root.username);
    await page.getByTestId(`actor-option-${root.username}`).click();

    const actionFilter = page.getByRole('combobox', { name: 'Action filter' });
    await actionFilter.fill('user.updated');
    await actionFilter.press('Enter');

    await page.getByLabel('Resource type filter').selectOption('user');
    await page.getByLabel('From date').fill(addDays(todayText(), -1));
    await page.getByLabel('To date').fill(addDays(todayText(), 1));

    const eventsResponse = page.waitForResponse(
      (response) =>
        response.url().includes('/api/v1/audit/events') &&
        response.url().includes('action=user.updated') &&
        response.url().includes('resource_type=user') &&
        response.request().method() === 'GET',
    );
    await page.getByRole('button', { name: 'Apply' }).click();
    const response = await eventsResponse;
    expect(response.status()).toBe(200);

    const auditRow = page
      .locator('tr[data-testid^="audit-row-"]:not([data-testid^="audit-row-detail-"])')
      .filter({ hasText: 'user.updated' })
      .filter({ hasText: root.username })
      .first();

    await expect(auditRow).toBeVisible();
    await expect(auditRow).toContainText('user.updated');
    await expect(auditRow).toContainText('user/');

    await auditRow.click();
    const detail = page.locator('[data-testid^="audit-row-detail-"]').first();
    await expect(detail).toContainText(user.username);
    await expect(detail).toContainText('scheduler');
  });

  test('root 可以用 action resource type 和日期找到使用者停用紀錄', async ({ page, request }) => {
    const root = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      root === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to run audit log tests.',
    );

    const user = createTestUser('audit_deactivate');
    await registerUser(request, user);

    await loginViaUi(page, root);
    await page.goto('/users');
    await page.getByLabel('Search users').fill(user.username);

    const row = userRow(page, user.username);
    await expect(row).toBeVisible();
    await row.getByRole('button', { name: 'Deactivate' }).click();
    await expect(row).toContainText('Inactive');

    await page.goto('/audit');
    await expect(page.getByRole('heading', { name: 'Audit Log' })).toBeVisible();

    const actionFilter = page.getByRole('combobox', { name: 'Action filter' });
    await actionFilter.fill('user.deactivated');
    await actionFilter.press('Enter');

    await page.getByLabel('Resource type filter').selectOption('user');
    await page.getByLabel('From date').fill(addDays(todayText(), -1));
    await page.getByLabel('To date').fill(addDays(todayText(), 1));

    const eventsResponse = page.waitForResponse(
      (response) =>
        response.url().includes('/api/v1/audit/events') &&
        response.url().includes('action=user.deactivated') &&
        response.url().includes('resource_type=user') &&
        response.request().method() === 'GET',
    );
    await page.getByRole('button', { name: 'Apply' }).click();
    const response = await eventsResponse;
    expect(response.status()).toBe(200);

    const auditRow = page
      .locator('tr[data-testid^="audit-row-"]:not([data-testid^="audit-row-detail-"])')
      .filter({ hasText: 'user.deactivated' })
      .filter({ hasText: root.username })
      .first();

    await expect(auditRow).toBeVisible();
    await expect(auditRow).toContainText('user.deactivated');
    await expect(auditRow).toContainText('user/');

    await auditRow.click();
    const detail = page.locator('[data-testid^="audit-row-detail-"]').first();
    await expect(detail).toContainText('is_active');
    await expect(detail).toContainText('true');
    await expect(detail).toContainText('false');
  });

  test('root 可以用 action resource type 和日期找到訂單更新紀錄', async ({ page, request }) => {
    const root = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      root === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to run audit log tests.',
    );

    const orderManager = await createUserWithRole(
      request,
      root,
      'order_manager',
      'audit_order_update',
    );
    const suffix = uniqueSuffix();
    const customerName = `E2E Audit Order ${suffix}`;
    const updatedNotes = `audit notes ${suffix}`;

    await loginViaUi(page, orderManager);
    await page.getByRole('link', { name: 'Orders' }).click();
    await createOrderViaUi(page, {
      customerName,
      waferQuantity: '125',
      requestedDeliveryDate: '2026-06-30',
    });
    await page.getByTestId('orders-search-input').fill(customerName);

    const row = orderRow(page, customerName);
    await expect(row).toBeVisible();
    await row.getByTestId('orders-edit-button').click();

    const modal = page.getByTestId('order-modal');
    await expect(modal).toBeVisible();
    await page.getByTestId('order-notes-input').fill(updatedNotes);
    await page.getByTestId('order-modal-submit-button').click();
    await expect(modal).toBeHidden();

    await page.getByRole('button', { name: 'Logout' }).click();
    await expect(page).toHaveURL(/\/login/);

    await loginViaUi(page, root);
    await page.goto('/audit');
    await expect(page.getByRole('heading', { name: 'Audit Log' })).toBeVisible();

    const actionFilter = page.getByRole('combobox', { name: 'Action filter' });
    await actionFilter.fill('order.updated');
    await actionFilter.press('Enter');

    await page.getByLabel('Resource type filter').selectOption('order');
    await page.getByLabel('From date').fill(addDays(todayText(), -1));
    await page.getByLabel('To date').fill(addDays(todayText(), 1));

    const eventsResponse = page.waitForResponse(
      (response) =>
        response.url().includes('/api/v1/audit/events') &&
        response.url().includes('action=order.updated') &&
        response.url().includes('resource_type=order') &&
        response.request().method() === 'GET',
    );
    await page.getByRole('button', { name: 'Apply' }).click();
    const response = await eventsResponse;
    expect(response.status()).toBe(200);

    const auditRow = page
      .locator('tr[data-testid^="audit-row-"]:not([data-testid^="audit-row-detail-"])')
      .filter({ hasText: 'order.updated' })
      .filter({ hasText: orderManager.username })
      .first();

    await expect(auditRow).toBeVisible();
    await expect(auditRow).toContainText('order.updated');
    await expect(auditRow).toContainText('order/');

    await auditRow.click();
    const detail = page.locator('[data-testid^="audit-row-detail-"]').first();
    await expect(detail).toContainText('notes');
    await expect(detail).toContainText(updatedNotes);
  });
});
