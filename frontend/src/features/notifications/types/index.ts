export interface NotificationResponse {
  id: string;
  user_id: string;
  order_id: string | null;
  type: string;
  message: string;
  is_read: boolean;
  created_at: string;
}

export interface NotificationListResponse {
  items: NotificationResponse[];
  total: number;
}
