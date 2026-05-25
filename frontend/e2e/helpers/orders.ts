import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';

export interface OrderFormInput {
  customerName: string;
  waferQuantity: string;
  requestedDeliveryDate: string;
}

export interface OrderApiItem {
  id: string;
  order_number: string;
  customer_name: string;
  status: string;
  is_processing_locked: boolean;
}

interface OrderListResponse {
  items: OrderApiItem[];
}

export async function createOrderViaUi(page: Page, order: OrderFormInput): Promise<void> {
  await page.getByTestId('orders-create-button').click();
  await expect(page.getByTestId('order-modal')).toBeVisible();

  const customerInput = page.getByTestId('order-customer-name-input');
  const quantityInput = page.getByTestId('order-wafer-quantity-input');
  const deliveryDateInput = page.getByTestId('order-requested-delivery-date-input');

  for (let attempt = 0; attempt < 2; attempt += 1) {
    for (let fillAttempt = 0; fillAttempt < 3; fillAttempt += 1) {
      await customerInput.fill(order.customerName);
      await deliveryDateInput.fill(order.requestedDeliveryDate);
      await quantityInput.fill(order.waferQuantity);

      const [customerName, waferQuantity, requestedDeliveryDate] = await Promise.all([
        customerInput.inputValue(),
        quantityInput.inputValue(),
        deliveryDateInput.inputValue(),
      ]);

      if (
        customerName === order.customerName &&
        waferQuantity === order.waferQuantity &&
        requestedDeliveryDate === order.requestedDeliveryDate
      ) {
        break;
      }

      await page.waitForTimeout(100);
    }

    await page.getByTestId('order-modal-submit-button').click();

    const closed = await page
      .getByTestId('order-modal')
      .waitFor({ state: 'hidden', timeout: 3000 })
      .then(() => true)
      .catch(() => false);
    if (closed) return;
  }

  await expect(page.getByTestId('order-modal')).toBeHidden();
}

export function orderRow(page: Page, customerName: string): Locator {
  return page.getByRole('row').filter({ hasText: customerName });
}

export async function reloadOrdersPage(page: Page): Promise<void> {
  await page.reload();
  await expect(page).toHaveURL('/orders');
  await expect(page.getByTestId('orders-page')).toBeVisible();
  await expect(page.getByTestId('orders-search-input')).toBeVisible();
}

export async function findOrderByCustomer(
  request: APIRequestContext,
  customerName: string,
): Promise<OrderApiItem> {
  const response = await request.get('/api/v1/orders', {
    params: {
      search: customerName,
      page_size: 100,
    },
  });

  if (!response.ok()) {
    throw new Error(`Failed to find order: ${response.status()} ${await response.text()}`);
  }

  const payload = (await response.json()) as OrderListResponse;
  const order = payload.items.find((item) => item.customer_name === customerName);

  if (!order) {
    throw new Error(`Could not find order for customer ${customerName}`);
  }

  return order;
}

export async function waitForOrderUnlocked(
  request: APIRequestContext,
  customerName: string,
): Promise<OrderApiItem> {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const order = await findOrderByCustomer(request, customerName);
    if (!order.is_processing_locked) return order;
    await new Promise((resolve) => {
      setTimeout(resolve, 500);
    });
  }

  throw new Error(`Order stayed locked for customer ${customerName}`);
}
