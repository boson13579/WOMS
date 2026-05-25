import type { APIRequestContext } from '@playwright/test';

import { loginViaApi, type LoginUser } from './auth';
import { dateFromToday, uniqueSuffix } from './data';

interface OrderResponse {
  id: string;
  order_number: string;
  version_id: number;
  is_processing_locked: boolean;
}

async function createOrderViaApi(request: APIRequestContext): Promise<OrderResponse> {
  const response = await request.post('/api/v1/orders', {
    data: {
      customer_name: `E2E Notification ${uniqueSuffix()}`,
      wafer_quantity: 125,
      requested_delivery_date: dateFromToday(20),
    },
  });

  if (!response.ok()) {
    throw new Error(
      `Failed to create notification setup order: ${response.status()} ${await response.text()}`,
    );
  }

  return (await response.json()) as OrderResponse;
}

async function waitForOrderUnlocked(
  request: APIRequestContext,
  orderId: string,
): Promise<OrderResponse> {
  const deadline = Date.now() + 60_000;

  while (Date.now() < deadline) {
    const response = await request.get(`/api/v1/orders/${orderId}`);
    if (!response.ok()) {
      throw new Error(
        `Failed to poll order lock state: ${response.status()} ${await response.text()}`,
      );
    }

    const order = (await response.json()) as OrderResponse;
    if (!order.is_processing_locked) {
      return order;
    }

    await new Promise((resolve) => {
      setTimeout(resolve, 1000);
    });
  }

  throw new Error(`Order ${orderId} did not unlock before timeout.`);
}

export async function createUnreadNotificationViaOrderUpdate(
  request: APIRequestContext,
  user: LoginUser,
): Promise<string> {
  await loginViaApi(request, user);
  const createdOrder = await createOrderViaApi(request);
  let unlockedOrder = await waitForOrderUnlocked(request, createdOrder.id);

  for (let attempt = 0; attempt < 3; attempt += 1) {
    const response = await request.patch(`/api/v1/orders/${unlockedOrder.id}`, {
      data: {
        wafer_quantity: 150 + attempt,
        version_id: unlockedOrder.version_id,
      },
    });

    if (response.ok()) {
      return unlockedOrder.order_number;
    }

    if (response.status() !== 409 || attempt === 2) {
      throw new Error(
        `Failed to trigger notification: ${response.status()} ${await response.text()}`,
      );
    }

    unlockedOrder = await waitForOrderUnlocked(request, createdOrder.id);
  }

  return unlockedOrder.order_number;
}
