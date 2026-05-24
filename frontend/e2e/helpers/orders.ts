import { expect, type Locator, type Page } from '@playwright/test';

export interface OrderFormInput {
  customerName: string;
  waferQuantity: string;
  requestedDeliveryDate: string;
}

export async function createOrderViaUi(page: Page, order: OrderFormInput): Promise<void> {
  await page.getByTestId('orders-create-button').click();
  await expect(page.getByTestId('order-modal')).toBeVisible();

  for (let attempt = 0; attempt < 2; attempt += 1) {
    await page.getByTestId('order-wafer-quantity-input').fill(order.waferQuantity);
    await page.getByTestId('order-requested-delivery-date-input').fill(order.requestedDeliveryDate);
    await page.getByTestId('order-customer-name-input').fill(order.customerName);

    await expect(page.getByTestId('order-customer-name-input')).toHaveValue(order.customerName);
    await expect(page.getByTestId('order-wafer-quantity-input')).toHaveValue(order.waferQuantity);
    await expect(page.getByTestId('order-requested-delivery-date-input')).toHaveValue(
      order.requestedDeliveryDate,
    );

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
