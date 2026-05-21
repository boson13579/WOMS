import {
  AlertCircle,
  Check,
  CheckCheck,
  FileEdit,
  Inbox,
  Loader2,
  Lock,
  XCircle,
} from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';

import { Header } from '@/components/layout/Header';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { cn } from '@/lib/utils';

import {
  useMarkAllNotificationsRead,
  useMarkNotificationRead,
  useNotifications,
} from '../api/notifications';
import { useNotificationsWs } from '../hooks/useNotificationsWs';
import type { NotificationResponse } from '../types';

function getNotificationLabel(type: string): string {
  if (type === 'order_locked') return '訂單鎖定';
  if (type === 'order_status_changed') return '狀態變更';
  if (type === 'order_cancelled') return '訂單取消';
  return '系統消息';
}

function formatRelativeTime(dateStr: string): string {
  try {
    const date = new Date(dateStr);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    if (diffMs < 0) return '剛剛';

    const diffMins = Math.floor(diffMs / 60000);
    if (diffMins < 1) return '剛剛';
    if (diffMins < 60) return `${diffMins} 分鐘前`;

    const diffHours = Math.floor(diffMins / 60);
    if (diffHours < 24) return `${diffHours} 小時前`;

    const diffDays = Math.floor(diffHours / 24);
    if (diffDays === 1) return '昨天';
    if (diffDays < 7) return `${diffDays} 天前`;

    return date.toLocaleString('zh-TW', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return '未知時間';
  }
}

export function NotificationsPage(): JSX.Element {
  const [activeTab, setActiveTab] = useState<'unread' | 'all'>('unread');

  // Activate WebSocket listener: any notification.created event automatically invalidates cache
  useNotificationsWs();

  // Queries
  const { data: notificationsData, isPending } = useNotifications({
    all: activeTab === 'all',
  });

  // Fetch unread count exclusively for total header count
  const { data: unreadData } = useNotifications({ all: false });

  // Mutations
  const markRead = useMarkNotificationRead();
  const markAllRead = useMarkAllNotificationsRead();

  const handleMarkSingleRead = (id: string): void => {
    markRead.mutate(id, {
      onSuccess: () => {
        toast.success('已標記為已讀');
      },
      onError: (err) => {
        toast.error('操作失敗', { description: err.message });
      },
    });
  };

  const handleMarkAllRead = (): void => {
    markAllRead.mutate(undefined, {
      onSuccess: (res) => {
        toast.success('已將所有通知標記為已讀', {
          description: `成功更新了 ${res.updated} 筆通知。`,
        });
      },
      onError: (err) => {
        toast.error('操作失敗', { description: err.message });
      },
    });
  };

  const listItems = notificationsData?.items ?? [];
  const unreadCount = unreadData?.total ?? 0;

  // Icon selector based on notification type
  const getNotificationIcon = (type: string): JSX.Element => {
    switch (type) {
      case 'order_locked':
        return (
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-amber-50 text-amber-600 dark:bg-amber-950/30 dark:text-amber-400">
            <Lock className="h-4.5 w-4.5" />
          </div>
        );
      case 'order_status_changed':
        return (
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-sky-50 text-sky-600 dark:bg-sky-950/30 dark:text-sky-400">
            <FileEdit className="h-4.5 w-4.5" />
          </div>
        );
      case 'order_cancelled':
        return (
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-rose-50 text-rose-600 dark:bg-rose-950/30 dark:text-rose-400">
            <XCircle className="h-4.5 w-4.5" />
          </div>
        );
      default:
        return (
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-secondary text-secondary-foreground">
            <AlertCircle className="h-4.5 w-4.5" />
          </div>
        );
    }
  };

  const renderContent = (): JSX.Element => {
    if (isPending) {
      return (
        <div className="flex h-64 flex-col items-center justify-center gap-2 text-muted-foreground">
          <Loader2 className="h-8 w-8 animate-spin text-primary" />
          <p className="text-sm">載入通知中...</p>
        </div>
      );
    }

    if (listItems.length === 0) {
      return (
        <Card className="flex h-64 flex-col items-center justify-center gap-4 bg-muted/20 border-dashed p-6 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-muted text-muted-foreground/60">
            <Inbox className="h-6 w-6" />
          </div>
          <div className="space-y-1">
            <p className="text-sm font-medium">目前沒有通知</p>
            <p className="text-xs text-muted-foreground">
              {activeTab === 'unread' ? '您已讀完了所有系統通知！' : '目前尚無任何系統通知紀錄。'}
            </p>
          </div>
        </Card>
      );
    }

    return (
      <div className="space-y-3">
        {listItems.map((item: NotificationResponse) => {
          const cardClass = cn(
            'group flex items-start gap-4 p-4 transition-all duration-200 hover:shadow-md hover:shadow-muted/20',
            !item.is_read
              ? 'border-l-4 border-l-primary bg-primary/[0.01] dark:bg-primary/[0.005]'
              : 'bg-card/60 opacity-80 hover:opacity-100',
          );
          const textClass = cn(
            'text-sm leading-relaxed text-foreground/90 break-words',
            !item.is_read && 'font-medium text-foreground',
          );

          return (
            <Card key={item.id} className={cardClass}>
              {getNotificationIcon(item.type)}

              <div className="flex-1 min-w-0 space-y-1">
                <div className="flex items-center justify-between gap-2">
                  <p className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">
                    {getNotificationLabel(item.type)}
                  </p>
                  <span className="text-[10px] text-muted-foreground/80">
                    {formatRelativeTime(item.created_at)}
                  </span>
                </div>
                <p className={textClass}>{item.message}</p>
              </div>

              {!item.is_read && (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  onClick={() => {
                    handleMarkSingleRead(item.id);
                  }}
                  disabled={markRead.isPending}
                  className="h-8 w-8 text-muted-foreground hover:bg-primary/10 hover:text-primary transition-colors opacity-100 sm:opacity-0 group-hover:opacity-100"
                  title="標記為已讀"
                >
                  {markRead.isPending && markRead.variables === item.id ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Check className="h-4 w-4" />
                  )}
                </Button>
              )}
            </Card>
          );
        })}
      </div>
    );
  };

  return (
    <>
      <Header title="系統通知" />

      <div className="mx-auto max-w-4xl px-6 py-6 space-y-6">
        {/* Top Controls */}
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex rounded-lg bg-muted p-1 w-full sm:w-[260px]">
            <button
              type="button"
              onClick={() => {
                setActiveTab('unread');
              }}
              className={cn(
                'relative flex flex-1 items-center justify-center rounded-md px-3 py-1.5 text-sm font-medium transition-all duration-200 shrink-0',
                activeTab === 'unread'
                  ? 'bg-background text-foreground shadow-sm'
                  : 'text-muted-foreground hover:bg-background/40 hover:text-foreground',
              )}
            >
              未讀通知
              {unreadCount > 0 && (
                <span className="ml-1.5 rounded-full bg-rose-500 px-1.5 py-0.5 text-[10px] font-bold text-white leading-none">
                  {unreadCount}
                </span>
              )}
            </button>
            <button
              type="button"
              onClick={() => {
                setActiveTab('all');
              }}
              className={cn(
                'flex flex-1 items-center justify-center rounded-md px-3 py-1.5 text-sm font-medium transition-all duration-200 shrink-0',
                activeTab === 'all'
                  ? 'bg-background text-foreground shadow-sm'
                  : 'text-muted-foreground hover:bg-background/40 hover:text-foreground',
              )}
            >
              全部通知
            </button>
          </div>

          {unreadCount > 0 && (
            <Button
              variant="outline"
              size="sm"
              onClick={handleMarkAllRead}
              disabled={markAllRead.isPending}
              className="flex items-center gap-1.5 border-dashed border-primary/40 hover:border-primary shrink-0 transition-colors"
            >
              {markAllRead.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <CheckCheck className="h-3.5 w-3.5" />
              )}
              全部標記為已讀
            </Button>
          )}
        </div>

        <Separator />

        {/* Notifications list */}
        {renderContent()}
      </div>
    </>
  );
}
