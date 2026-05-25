import { expect, type Locator, type Page } from '@playwright/test';

export interface OrderFormInput {
  customerName: string;
  waferQuantity: string;
  requestedDeliveryDate: string;
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
