import { expect, test } from '@playwright/test';

import {
  createTestUser,
  createUserWithRole,
  getEnvUser,
  loginViaApi,
  loginViaUi,
  registerUser,
} from '../helpers/auth';
import { createOrderViaUi, orderRow } from '../helpers/orders';

test.describe('RBAC', () => {
  test('scheduler 可以看到排程按鈕', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const scheduler = await createUserWithRole(request, admin, 'scheduler', 'rbac_scheduler');

    await loginViaUi(page, scheduler);
    await page.getByRole('link', { name: 'Orders' }).click();

    await expect(page.getByTestId('orders-create-button')).toBeVisible();
    await expect(page.getByTestId('orders-calendar-button')).toBeVisible();
    await expect(page.getByTestId('orders-schedule-button')).toBeVisible();
  });

  test('order manager 看不到排程按鈕', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const orderManager = await createUserWithRole(
      request,
      admin,
      'order_manager',
      'rbac_order_manager',
    );

    await loginViaUi(page, orderManager);
    await page.getByRole('link', { name: 'Orders' }).click();

    await expect(page.getByTestId('orders-create-button')).toBeVisible();
    await expect(page.getByTestId('orders-calendar-button')).toBeVisible();
    await expect(page.getByTestId('orders-schedule-button')).toBeHidden();
  });

  test('viewer 看不到訂單寫入與排程操作', async ({ page, request }) => {
    const viewer = createTestUser('rbac_viewer');
    await registerUser(request, viewer);

    await loginViaUi(page, viewer);
    await page.getByRole('link', { name: 'Orders' }).click();

    await expect(page.getByTestId('orders-create-button')).toBeHidden();
    await expect(page.getByTestId('orders-calendar-button')).toBeHidden();
    await expect(page.getByTestId('orders-schedule-button')).toBeHidden();
  });

  test('viewer 直接呼叫新增訂單後端介面會被拒絕', async ({ request }) => {
    const viewer = createTestUser('rbac_api_viewer_create');
    await registerUser(request, viewer);
    await loginViaApi(request, viewer);

    const response = await request.post('/api/v1/orders', {
      data: {
        customer_name: `E2E Forbidden Create ${Date.now()}`,
        wafer_quantity: 125,
        requested_delivery_date: '2026-06-30',
      },
    });

    expect(response.status()).toBe(403);
  });

  test('order manager 直接呼叫觸發排程後端介面會被拒絕', async ({ request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const orderManager = await createUserWithRole(
      request,
      admin,
      'order_manager',
      'rbac_api_schedule',
    );
    await loginViaApi(request, orderManager);

    const response = await request.post('/api/v1/schedule/trigger');

    expect(response.status()).toBe(403);
  });

  test('order manager 不能編輯其他 order manager 建立的訂單', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const owner = await createUserWithRole(request, admin, 'order_manager', 'rbac_owner');
    const otherManager = await createUserWithRole(request, admin, 'order_manager', 'rbac_other');
    const suffix = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const customerName = `E2E RBAC Owned ${suffix}`;

    await loginViaUi(page, owner);
    await page.getByRole('link', { name: 'Orders' }).click();
    await createOrderViaUi(page, {
      customerName,
      waferQuantity: '125',
      requestedDeliveryDate: '2026-06-30',
    });

    await page.getByRole('button', { name: 'Logout' }).click();
    await expect(page).toHaveURL(/\/login/);

    await loginViaUi(page, otherManager);
    await page.getByRole('link', { name: 'Orders' }).click();
    await page.getByTestId('orders-search-input').fill(customerName);

    const row = orderRow(page, customerName);
    await expect(row).toBeVisible();
    await expect(row.getByTestId('orders-edit-button')).toBeHidden();
    await expect(row.getByTestId('orders-delete-button')).toBeHidden();
  });

  test('非 root 使用者進入使用者管理頁會被導回首頁', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const scheduler = await createUserWithRole(request, admin, 'scheduler', 'rbac_non_root_users');

    await loginViaUi(page, scheduler);
    await page.goto('/users');

    await expect(page).toHaveURL('/');
    await expect(page.getByRole('heading', { name: 'User Management' })).toBeHidden();
  });

  test('root 可以進入稽核紀錄頁面', async ({ page }) => {
    const root = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      root === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    await loginViaUi(page, root);
    await page.goto('/audit');

    await expect(page).toHaveURL('/audit');
    await expect(page.getByRole('heading', { name: 'Audit Log' })).toBeVisible();
  });

  test('scheduler 進入稽核紀錄頁面會被導回首頁', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const scheduler = await createUserWithRole(request, admin, 'scheduler', 'rbac_no_audit');

    await loginViaUi(page, scheduler);
    await page.goto('/audit');

    await expect(page).toHaveURL('/');
    await expect(page.getByRole('heading', { name: 'Audit Log' })).toBeHidden();
  });

  test('scheduler 可以進入監控頁但 viewer 會被導回首頁', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const scheduler = await createUserWithRole(request, admin, 'scheduler', 'rbac_observability');
    const viewer = createTestUser('rbac_no_observability');
    await registerUser(request, viewer);

    await loginViaUi(page, scheduler);
    await page.goto('/observability');

    await expect(page).toHaveURL('/observability');
    await expect(page.getByRole('heading', { name: 'Observability' })).toBeVisible();

    await page.getByRole('button', { name: 'Logout' }).click();
    await expect(page).toHaveURL(/\/login/);

    await loginViaUi(page, viewer);
    await page.goto('/observability');

    await expect(page).toHaveURL('/');
  });
});
