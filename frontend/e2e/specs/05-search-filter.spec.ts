import { expect, test } from '@playwright/test';

import { createUserWithRole, getEnvUser, loginViaUi } from '../helpers/auth';
import { dateFromToday } from '../helpers/data';
import { createOrderViaUi, orderRow } from '../helpers/orders';

test.describe('Order search and filters', () => {
  test('可以用客戶名稱搜尋訂單', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const orderManager = await createUserWithRole(
      request,
      admin,
      'order_manager',
      'search_order_manager',
    );
    const suffix = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const targetCustomer = `E2E Search Target ${suffix}`;
    const otherCustomer = `E2E Search Other ${suffix}`;

    await loginViaUi(page, orderManager);
    await page.getByRole('link', { name: 'Orders' }).click();
    await createOrderViaUi(page, {
      customerName: targetCustomer,
      waferQuantity: '125',
      requestedDeliveryDate: dateFromToday(20),
    });
    await createOrderViaUi(page, {
      customerName: otherCustomer,
      waferQuantity: '150',
      requestedDeliveryDate: dateFromToday(25),
    });

    await page.getByTestId('orders-search-input').fill(targetCustomer);

    await expect(orderRow(page, targetCustomer)).toBeVisible();
    await expect(orderRow(page, otherCustomer)).toBeHidden();
  });

  test('可以用待處理狀態篩選訂單', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const orderManager = await createUserWithRole(
      request,
      admin,
      'order_manager',
      'filter_order_manager',
    );
    const suffix = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const customerName = `E2E Filter Pending ${suffix}`;

    await loginViaUi(page, orderManager);
    await page.getByRole('link', { name: 'Orders' }).click();
    await createOrderViaUi(page, {
      customerName,
      waferQuantity: '125',
      requestedDeliveryDate: dateFromToday(20),
    });

    await page.getByTestId('orders-search-input').fill(customerName);
    await expect(orderRow(page, customerName)).toBeVisible();

    await page.getByTestId('orders-status-filter').selectOption('scheduled');
    await expect(orderRow(page, customerName)).toBeVisible();

    await page.getByTestId('orders-status-filter').selectOption('pending');
    await expect(orderRow(page, customerName)).toBeHidden();
  });

  test('可以重設搜尋與狀態篩選條件', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const orderManager = await createUserWithRole(
      request,
      admin,
      'order_manager',
      'reset_filter_order_manager',
    );
    const suffix = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const customerName = `E2E Reset Filter ${suffix}`;

    await loginViaUi(page, orderManager);
    await page.getByRole('link', { name: 'Orders' }).click();
    await createOrderViaUi(page, {
      customerName,
      waferQuantity: '125',
      requestedDeliveryDate: dateFromToday(20),
    });

    await page.getByTestId('orders-search-input').fill(customerName);
    await expect(orderRow(page, customerName)).toBeVisible();

    await page.getByTestId('orders-status-filter').selectOption('scheduled');
    await expect(orderRow(page, customerName)).toBeHidden();

    await page.getByTestId('orders-reset-filters-button').click();

    await expect(page.getByTestId('orders-search-input')).toHaveValue('');
    await expect(page.getByTestId('orders-status-filter')).toHaveValue('');
    await expect(page.getByRole('row').nth(1)).toBeVisible();
  });

  test('可以依客戶名稱排序訂單', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const orderManager = await createUserWithRole(
      request,
      admin,
      'order_manager',
      'sort_order_manager',
    );
    const suffix = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const searchPrefix = `E2E Sort ${suffix}`;
    const firstCustomer = `${searchPrefix} Alpha`;
    const secondCustomer = `${searchPrefix} Zulu`;
    const matchingRows = page.getByRole('row').filter({ hasText: searchPrefix });

    await loginViaUi(page, orderManager);
    await page.getByRole('link', { name: 'Orders' }).click();
    await createOrderViaUi(page, {
      customerName: firstCustomer,
      waferQuantity: '125',
      requestedDeliveryDate: dateFromToday(20),
    });
    await createOrderViaUi(page, {
      customerName: secondCustomer,
      waferQuantity: '150',
      requestedDeliveryDate: dateFromToday(25),
    });

    await page.getByTestId('orders-search-input').fill(searchPrefix);
    await expect(matchingRows).toHaveCount(2);

    await page.getByTestId('orders-sort-customer-name').click();
    await expect(matchingRows.nth(0)).toContainText(firstCustomer);
    await expect(matchingRows.nth(1)).toContainText(secondCustomer);

    await page.getByTestId('orders-sort-customer-name').click();
    await expect(matchingRows.nth(0)).toContainText(secondCustomer);
    await expect(matchingRows.nth(1)).toContainText(firstCustomer);
  });

  test('可以依需求交期排序訂單', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const orderManager = await createUserWithRole(
      request,
      admin,
      'order_manager',
      'sort_delivery_manager',
    );
    const suffix = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const searchPrefix = `E2E Delivery Sort ${suffix}`;
    const earlierCustomer = `${searchPrefix} Earlier`;
    const laterCustomer = `${searchPrefix} Later`;
    const matchingRows = page.getByRole('row').filter({ hasText: searchPrefix });

    await loginViaUi(page, orderManager);
    await page.getByRole('link', { name: 'Orders' }).click();
    await createOrderViaUi(page, {
      customerName: laterCustomer,
      waferQuantity: '150',
      requestedDeliveryDate: dateFromToday(25),
    });
    await createOrderViaUi(page, {
      customerName: earlierCustomer,
      waferQuantity: '125',
      requestedDeliveryDate: dateFromToday(20),
    });

    await page.getByTestId('orders-search-input').fill(searchPrefix);
    await expect(matchingRows).toHaveCount(2);

    const requestedDeliveryDateHeader = page.getByTestId('orders-page').getByText('要求交貨日');

    await requestedDeliveryDateHeader.click();
    await expect(matchingRows.nth(0)).toContainText(earlierCustomer);
    await expect(matchingRows.nth(1)).toContainText(laterCustomer);

    await requestedDeliveryDateHeader.click();
    await expect(matchingRows.nth(0)).toContainText(laterCustomer);
    await expect(matchingRows.nth(1)).toContainText(earlierCustomer);
  });

  test('可以依晶圓數量排序訂單', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const orderManager = await createUserWithRole(
      request,
      admin,
      'order_manager',
      'sort_quantity_manager',
    );
    const suffix = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const searchPrefix = `E2E Quantity Sort ${suffix}`;
    const smallerCustomer = `${searchPrefix} Small`;
    const largerCustomer = `${searchPrefix} Large`;
    const matchingRows = page.getByRole('row').filter({ hasText: searchPrefix });

    await loginViaUi(page, orderManager);
    await page.getByRole('link', { name: 'Orders' }).click();
    await createOrderViaUi(page, {
      customerName: largerCustomer,
      waferQuantity: '500',
      requestedDeliveryDate: dateFromToday(25),
    });
    await createOrderViaUi(page, {
      customerName: smallerCustomer,
      waferQuantity: '125',
      requestedDeliveryDate: dateFromToday(20),
    });

    await page.getByTestId('orders-search-input').fill(searchPrefix);
    await expect(matchingRows).toHaveCount(2);

    const waferQuantityHeader = page.getByTestId('orders-page').getByText('晶圓數量');

    await waferQuantityHeader.click();
    await expect(matchingRows.nth(0)).toContainText(smallerCustomer);
    await expect(matchingRows.nth(1)).toContainText(largerCustomer);

    await waferQuantityHeader.click();
    await expect(matchingRows.nth(0)).toContainText(largerCustomer);
    await expect(matchingRows.nth(1)).toContainText(smallerCustomer);
  });

  test('可以用訂單編號搜尋訂單', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const orderManager = await createUserWithRole(
      request,
      admin,
      'order_manager',
      'order_number_search_manager',
    );
    const suffix = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const customerName = `E2E Order Number Search ${suffix}`;

    await loginViaUi(page, orderManager);
    await page.getByRole('link', { name: 'Orders' }).click();
    await createOrderViaUi(page, {
      customerName,
      waferQuantity: '125',
      requestedDeliveryDate: dateFromToday(20),
    });

    await page.getByTestId('orders-search-input').fill(customerName);

    const createdRow = orderRow(page, customerName);
    await expect(createdRow).toBeVisible();
    const orderNumber = (await createdRow.getByRole('cell').first().innerText()).trim();

    await page.getByTestId('orders-search-input').fill(orderNumber);

    await expect(orderRow(page, customerName)).toBeVisible();
    await expect(page.getByRole('row').filter({ hasText: orderNumber })).toHaveCount(1);
  });

  test('搜尋沒有符合結果時會顯示空狀態', async ({ page, request }) => {
    const admin = getEnvUser('E2E_ADMIN_USERNAME', 'E2E_ADMIN_PASSWORD', 'admin');
    test.skip(
      admin === null,
      'Set E2E_ADMIN_PASSWORD, and optionally E2E_ADMIN_USERNAME, to create role-specific users.',
    );

    const orderManager = await createUserWithRole(
      request,
      admin,
      'order_manager',
      'empty_search_manager',
    );
    const suffix = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;

    await loginViaUi(page, orderManager);
    await page.getByRole('link', { name: 'Orders' }).click();
    await page.getByTestId('orders-search-input').fill(`E2E No Such Order ${suffix}`);

    await expect(page.getByTestId('orders-empty-state')).toBeVisible();
  });
});
