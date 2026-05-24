import { expect, type Locator, type Page } from '@playwright/test';

export interface OrderFormInput {
  customerName: string;
  waferQuantity: string;
  requestedDeliveryDate: string;
}

export async function createOrderViaUi(page: Page, order: OrderFormInput): Promise<void> {
  await page.getByTestId('orders-create-button').click();
  await expect(page.getByTestId('order-modal')).toBeVisible();

  await page.getByTestId('order-customer-name-input').fill(order.customerName);
  await page.getByTestId('order-wafer-quantity-input').fill(order.waferQuantity);
  await page.getByTestId('order-requested-delivery-date-input').fill(order.requestedDeliveryDate);
  await page.getByTestId('order-modal-submit-button').click();

  await expect(page.getByTestId('order-modal')).toBeHidden();
}

export function orderRow(page: Page, customerName: string): Locator {
  return page.getByRole('row').filter({ hasText: customerName });
}
