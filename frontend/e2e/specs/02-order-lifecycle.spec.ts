import { expect, test } from '@playwright/test';

import {
  createTestUser,
  createUserWithRole,
  getEnvUser,
  loginViaUi,
  registerUser,
} from '../helpers/auth';
import { dateFromToday, uniqueSuffix } from '../helpers/data';
import {
  createOrderViaUi,
  orderRow,
  reloadOrdersPage,
  waitForOrderUnlocked,
} from '../helpers/orders';

test.describe('Order access', () => {
  test('viewer 可以進入訂單頁但看不到寫入操作', async ({ page, request }) => {
    const user = createTestUser('orders_viewer');
    await registerUser(request, user);
    await loginViaUi(page, user);

    await page.getByRole('link', { name: 'Orders' }).click();

    await expect(page).toHaveURL('/orders');
    await expect(page.getByTestId('orders-page')).toBeVisible();
    await expect(page.getByTestId('orders-create-button')).toBeHidden();
    await expect(page.getByTestId('orders-calendar-button')).toBeHidden();
    await expect(page.getByTestId('orders-schedule-button')).toBeHidden();
  });

  test('order manager 可以建立訂單並在列表看到該訂單', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const orderManager = await createUserWithRole(
      request,
      admin,
      'order_manager',
      'orders_manager',
    );
    const suffix = uniqueSuffix();
    const customerName = `E2E Customer ${suffix}`;
    const order = {
      customerName,
      waferQuantity: '125',
      requestedDeliveryDate: dateFromToday(20),
    };

    await loginViaUi(page, orderManager);
    await page.getByRole('link', { name: 'Orders' }).click();

    await createOrderViaUi(page, order);

    await page.getByTestId('orders-search-input').fill(customerName);

    const row = orderRow(page, customerName);
    await expect(row).toBeVisible();
    await expect(row.getByRole('cell', { name: '125' })).toBeVisible();
  });

  test('order manager 可以編輯自己建立的訂單', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const orderManager = await createUserWithRole(request, admin, 'order_manager', 'orders_editor');
    const suffix = uniqueSuffix();
    const customerName = `E2E Edit ${suffix}`;

    await loginViaUi(page, orderManager);
    await page.getByRole('link', { name: 'Orders' }).click();
    await createOrderViaUi(page, {
      customerName,
      waferQuantity: '125',
      requestedDeliveryDate: dateFromToday(20),
    });
    await waitForOrderUnlocked(request, customerName);
    await reloadOrdersPage(page);
    await page.getByTestId('orders-search-input').fill(customerName);

    const row = orderRow(page, customerName);
    await expect(row).toBeVisible();
    await row.getByTestId('orders-edit-button').click();

    await expect(page.getByTestId('order-modal')).toBeVisible();
    await expect(page.getByTestId('order-customer-name-input')).toHaveValue(customerName);
    await expect(page.getByTestId('order-customer-name-input')).toBeDisabled();
    await expect(page.getByTestId('order-wafer-quantity-input')).toHaveValue('125');
    await expect(page.getByTestId('order-requested-delivery-date-input')).toHaveValue(
      dateFromToday(20),
    );
    await page.getByTestId('order-wafer-quantity-input').fill('250');
    await page.getByTestId('order-requested-delivery-date-input').fill(dateFromToday(25));
    await page.getByTestId('order-modal-submit-button').click();

    await expect(page.getByTestId('order-modal')).toBeHidden();
    await expect(row.getByRole('cell', { name: '250' })).toBeVisible();
  });

  test('order manager 可以刪除自己建立的訂單', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const orderManager = await createUserWithRole(request, admin, 'order_manager', 'orders_delete');
    const suffix = uniqueSuffix();
    const customerName = `E2E Delete ${suffix}`;

    await loginViaUi(page, orderManager);
    await page.getByRole('link', { name: 'Orders' }).click();
    await createOrderViaUi(page, {
      customerName,
      waferQuantity: '125',
      requestedDeliveryDate: dateFromToday(20),
    });
    await page.getByTestId('orders-search-input').fill(customerName);

    const row = orderRow(page, customerName);
    await expect(row).toBeVisible();

    page.once('dialog', async (dialog) => {
      await dialog.accept();
    });
    await row.getByTestId('orders-delete-button').click();

    await expect(row).toBeHidden();
    await expect(page.getByTestId('orders-empty-state')).toBeVisible();
  });

  test('order manager 建立訂單時晶圓數量必須介於 25 和 2500', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const orderManager = await createUserWithRole(
      request,
      admin,
      'order_manager',
      'orders_validation',
    );
    const suffix = uniqueSuffix();

    await loginViaUi(page, orderManager);
    await page.getByRole('link', { name: 'Orders' }).click();
    await page.getByTestId('orders-create-button').click();

    const modal = page.getByTestId('order-modal');
    await expect(modal).toBeVisible();

    await page.getByTestId('order-customer-name-input').fill(`E2E Invalid Low ${suffix}`);
    await page.getByTestId('order-requested-delivery-date-input').fill(dateFromToday(20));
    await page.getByTestId('order-wafer-quantity-input').fill('24');
    await page.getByTestId('order-modal-submit-button').click();

    await expect(modal).toBeVisible();
    await expect(modal.getByRole('alert')).toBeVisible();

    await page.getByTestId('order-wafer-quantity-input').fill('2501');
    await page.getByTestId('order-modal-submit-button').click();

    await expect(modal).toBeVisible();
    await expect(modal.getByRole('alert')).toBeVisible();
  });

  test('order manager 建立訂單時必填欄位不可空白', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const orderManager = await createUserWithRole(
      request,
      admin,
      'order_manager',
      'orders_required_validation',
    );

    await loginViaUi(page, orderManager);
    await page.getByRole('link', { name: 'Orders' }).click();
    await page.getByTestId('orders-create-button').click();

    const modal = page.getByTestId('order-modal');
    await expect(modal).toBeVisible();

    await page.getByTestId('order-modal-submit-button').click();

    await expect(modal).toBeVisible();
    await expect(modal.getByText('請填寫客戶名稱')).toBeVisible();
    await expect(modal.getByText('請選擇要求交貨日')).toBeVisible();
  });

  test('order manager 建立訂單時負責人必須是系統使用者', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const orderManager = await createUserWithRole(
      request,
      admin,
      'order_manager',
      'orders_assignee_validation',
    );
    const suffix = uniqueSuffix();

    await loginViaUi(page, orderManager);
    await page.getByRole('link', { name: 'Orders' }).click();
    await page.getByTestId('orders-create-button').click();

    const modal = page.getByTestId('order-modal');
    await expect(modal).toBeVisible();

    await page.getByTestId('order-customer-name-input').fill(`E2E Invalid Assignee ${suffix}`);
    await page.getByTestId('order-wafer-quantity-input').fill('125');
    await page.getByTestId('order-requested-delivery-date-input').fill(dateFromToday(20));
    await modal.getByLabel('負責人').fill(`missing-assignee-${suffix}@example.com`);
    await page.getByTestId('order-modal-submit-button').click();

    await expect(modal).toBeVisible();
    await expect(modal.getByText('負責人必須是系統中現有的使用者')).toBeVisible();
  });
});
