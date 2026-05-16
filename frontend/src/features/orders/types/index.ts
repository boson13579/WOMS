/**
 * Order domain types shared across the orders feature.
 * Mirrors the backend Pydantic schemas exactly so Zod can parse them safely.
 */

export type OrderStatus = 'pending' | 'scheduled' | 'in_production' | 'completed' | 'cancelled';

export type SortField =
  | 'order_number'
  | 'customer_name'
  | 'wafer_quantity'
  | 'requested_delivery_date';

/** A single order record — matches backend OrderResponse */
export interface Order {
  id: string;
  order_number: string;
  customer_name: string;
  wafer_quantity: number;
  requested_delivery_date: string;
  scheduled_production_date: string | null;
  expected_delivery_date: string | null;
  status: OrderStatus;
  assigned_to: string | null;
  created_by: string;
  notes: string | null;
  pinned_production_date: string | null;
  is_pinned: boolean;
  is_processing_locked: boolean;
  version_id: number;
  created_at: string;
  updated_at: string;
}

export interface OrderListResponse {
  items: Order[];
  total: number;
  page: number;
  page_size: number;
}

/** Matches backend CreateOrderRequest */
export interface OrderCreate {
  customer_name: string;
  wafer_quantity: number;
  requested_delivery_date: string;
  assigned_to?: string | null;
  notes?: string | null;
}

/** Matches backend UpdateOrderRequest — version_id is required for optimistic lock */
export interface OrderUpdate {
  wafer_quantity?: number | null;
  requested_delivery_date?: string | null;
  notes?: string | null;
  version_id: number;
}

export interface BatchUpdateRequest {
  order_ids: string[];
  requested_delivery_date: string;
}

export interface BatchUpdateResponse {
  updated_count: number;
  skipped_count: number;
  skipped_ids: string[];
}

export interface AuditLogEntry {
  id: string;
  action: string;
  user_id: string | null;
  resource_id: string;
  old_value: Record<string, unknown> | null;
  new_value: Record<string, unknown> | null;
  created_at: string;
}

/** Matches backend ScheduleTriggerResponse from POST /schedule/trigger */
export interface ScheduleTriggerResponse {
  task_id: string;
  message: string;
}
