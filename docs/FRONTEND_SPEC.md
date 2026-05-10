# WOMS Frontend 元件使用規範

> 基於 shadcn/ui + Radix UI + Tailwind CSS 架構。所有 UI 元件統一從 `@/components/ui/` 引入，不得自行重新刻一份同功能的樣式。

---

## 一、UI 元件對照表

| 類型 | 推薦寫法 | 禁止寫法 |
|---|---|---|
| Button | `@/components/ui/button` | 原生 `<button>` 加 class |
| Input | `@/components/ui/input` | 原生 `<input>` 加 class |
| Label | `@/components/ui/label`，搭配 `htmlFor` | 原生 `<label>` 加 class |
| Badge | `@/components/ui/badge` | 手刻 span 做狀態 pill |
| Card | `@/components/ui/card`（含子元件） | 自行組 div + shadow |
| Dialog (Modal) | `@/components/ui/dialog`（含子元件） | 原生 `<dialog>` 或自刻 overlay |
| Table | `@/components/ui/table`（含子元件） | 原生 `<table>` 加 class |
| Textarea | `@/components/ui/textarea` | 原生 `<textarea>` 加 class |
| Separator | `@/components/ui/separator` | `<hr>` 或 border-b div |
| Skeleton | `@/components/ui/skeleton` | 自刻 animate-pulse div |

---

## 二、各元件詳細規範

### Button

```tsx
import { Button } from '@/components/ui/button'
```

**Variants**

| variant | 使用時機 |
|---|---|
| `default` | 主要行動（送出、新增） |
| `destructive` | 刪除、不可逆操作 |
| `outline` | 次要行動（取消、返回） |
| `secondary` | 中性操作 |
| `ghost` | 表格 icon 按鈕、工具列 |
| `link` | 文字連結式按鈕 |

**Sizes**

| size | 使用時機 |
|---|---|
| `default` | 一般按鈕 |
| `sm` | Header、工具列 |
| `lg` | 表單送出（登入、主 CTA） |
| `icon` | 只含圖示的按鈕（edit、delete） |

**範例**

```tsx
// 表單送出（含 loading 狀態）
<Button type="submit" disabled={mutation.isPending}>
  {mutation.isPending ? '送出中…' : '送出'}
</Button>

// 表格 icon 操作
<Button variant="ghost" size="icon" onClick={() => onEdit(order)}>
  <Pencil className="h-4 w-4" />
</Button>

// 危險操作
<Button variant="destructive" onClick={handleDelete}>刪除</Button>
```

---

### Input

```tsx
import { Input } from '@/components/ui/input'
```

- 接受所有原生 `<input>` 屬性（`type`, `placeholder`, `disabled`, `required` 等）
- 必須搭配 `<Label htmlFor={id}>` 使用
- 有驗證錯誤時加 `aria-invalid="true"` 和 `aria-describedby={errorId}`

**範例**

```tsx
<div className="space-y-2">
  <Label htmlFor="customer_name">客戶名稱 *</Label>
  <Input
    id="customer_name"
    type="text"
    placeholder="請輸入客戶名稱"
    aria-invalid={!!errors.customer_name}
    aria-describedby="customer_name-error"
    {...register('customer_name')}
  />
  {errors.customer_name && (
    <p id="customer_name-error" role="alert" className="text-sm text-destructive">
      {errors.customer_name.message}
    </p>
  )}
</div>
```

---

### Label

```tsx
import { Label } from '@/components/ui/label'
```

- **必須** 搭配 `htmlFor` 對應到 Input/Textarea 的 `id`
- 必填欄位在文字後加 ` *`（中文介面）

**範例**

```tsx
<Label htmlFor="wafer_quantity">晶圓數量 *</Label>
<Input id="wafer_quantity" type="number" {...register('wafer_quantity', { valueAsNumber: true })} />
```

---

### Badge

```tsx
import { Badge } from '@/components/ui/badge'
```

**Variants**

| variant | 語意 | 顏色 |
|---|---|---|
| `default` | 一般標籤 | 深色（primary） |
| `secondary` | 次要標籤 | 灰色 |
| `destructive` | 錯誤、拒絕 | 紅色 |
| `outline` | 中性資訊 | 邊框灰 |
| `success` | 完成、成功 | 翠綠（emerald） |
| `warning` | 警告、待處理 | 琥珀（amber） |
| `info` | 資訊、進行中 | 天藍（sky） |

**訂單狀態對應（STATUS_VARIANT）**

```tsx
const STATUS_VARIANT: Record<OrderStatus, BadgeVariant> = {
  pending:       'warning',
  scheduled:     'info',
  in_production: 'default',
  completed:     'success',
  cancelled:     'destructive',
}

<Badge variant={STATUS_VARIANT[order.status]}>
  {STATUS_LABEL[order.status]}
</Badge>
```

---

### Card

```tsx
import { Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter } from '@/components/ui/card'
```

**子元件結構**

```tsx
<Card>
  <CardHeader>
    <CardTitle>標題</CardTitle>
    <CardDescription>描述文字</CardDescription>
  </CardHeader>
  <CardContent>
    {/* 主要內容 */}
  </CardContent>
  <CardFooter>
    {/* 操作按鈕（選填） */}
  </CardFooter>
</Card>
```

- 所有子元件均接受 `className` 進行覆寫
- Dashboard 指標卡片只用 `Card + CardContent`（不需 Header）

---

### Dialog（Modal）

```tsx
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog'
```

**Props**

| prop | 型別 | 說明 |
|---|---|---|
| `open` | `boolean` | 控制開關（受控） |
| `onOpenChange` | `(open: boolean) => void` | 關閉時的回呼 |

**範例**

```tsx
<Dialog open={isOpen} onOpenChange={onClose}>
  <DialogContent>
    <DialogHeader>
      <DialogTitle>新增訂單</DialogTitle>
    </DialogHeader>

    {/* 表單內容 */}
    <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
      {/* fields */}
    </form>

    <DialogFooter>
      <Button variant="outline" type="button" onClick={onClose}>取消</Button>
      <Button type="submit" form="order-form">送出</Button>
    </DialogFooter>
  </DialogContent>
</Dialog>
```

- 底層使用原生 `<dialog>` + `showModal()`，天然支援 focus trap 和 ESC 關閉
- **不要**在 DialogContent 外再包 overlay div

---

### Table

```tsx
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from '@/components/ui/table'
```

**範例**

```tsx
<Table>
  <TableHeader>
    <TableRow>
      <TableHead>訂單編號</TableHead>
      <TableHead>客戶</TableHead>
      <TableHead>狀態</TableHead>
    </TableRow>
  </TableHeader>
  <TableBody>
    {orders.map(order => (
      <TableRow key={order.id} data-state={selected === order.id ? 'selected' : undefined}>
        <TableCell>{order.order_number}</TableCell>
        <TableCell>{order.customer_name}</TableCell>
        <TableCell>
          <Badge variant={STATUS_VARIANT[order.status]}>{STATUS_LABEL[order.status]}</Badge>
        </TableCell>
      </TableRow>
    ))}
  </TableBody>
</Table>
```

- TableRow 支援 `data-state="selected"` 高亮
- 可排序欄位另包 `<SortableHead>` 元件（見 OrderTable.tsx）

---

### Textarea

```tsx
import { Textarea } from '@/components/ui/textarea'
```

- 接受所有原生 `<textarea>` 屬性
- 預設 `min-height: 60px`；需要更高時加 `rows` 屬性
- 必須搭配 `<Label htmlFor={id}>`

```tsx
<Label htmlFor="notes">備註</Label>
<Textarea id="notes" rows={3} placeholder="選填" {...register('notes')} />
```

---

### Separator

```tsx
import { Separator } from '@/components/ui/separator'
```

```tsx
// 水平（預設）
<Separator />

// 垂直
<Separator orientation="vertical" />
```

---

### Skeleton

```tsx
import { Skeleton } from '@/components/ui/skeleton'
```

```tsx
// 載入中骨架屏
<Skeleton className="h-4 w-full" />
<Skeleton className="h-10 w-32" />
```

---

## 三、表單開發規範

### 技術棧

- **React Hook Form** + **Zod** 為唯一認可的表單方案
- Schema 定義放在元件同目錄或 `types/index.ts`

### 標準結構

```tsx
const schema = z.object({
  customer_name: z.string().min(1, '客戶名稱為必填'),
  wafer_quantity: z.number({ invalid_type_error: '請輸入數字' }).int().positive(),
})

type FormValues = z.infer<typeof schema>

function MyForm() {
  const { register, handleSubmit, formState: { errors } } = useForm<FormValues>({
    resolver: zodResolver(schema),
  })

  return (
    <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
      <div className="space-y-2">
        <Label htmlFor="customer_name">客戶名稱 *</Label>
        <Input
          id="customer_name"
          aria-invalid={!!errors.customer_name}
          aria-describedby="customer_name-error"
          {...register('customer_name')}
        />
        {errors.customer_name && (
          <p id="customer_name-error" role="alert" className="text-sm text-destructive">
            {errors.customer_name.message}
          </p>
        )}
      </div>
    </form>
  )
}
```

### 欄位間距

| 情境 | Tailwind class |
|---|---|
| Label + Input 組合 | `space-y-2` |
| 不同欄位之間 | `space-y-4` 或 `space-y-5` |
| 多欄並排 | `grid grid-cols-2 gap-4` |

---

## 四、無障礙（Accessibility）規範

| 規則 | 說明 |
|---|---|
| Label `htmlFor` | 每個 Input / Textarea 都必須有對應的 Label |
| `aria-invalid` | 有驗證錯誤時加在 Input 上 |
| `aria-describedby` | 指向錯誤訊息元素的 id |
| `role="alert"` | 動態出現的錯誤訊息加此屬性 |
| `disabled` | mutation 進行中時禁用送出按鈕 |

---

## 五、設計 Token 使用

優先用語意化 token，不要直接寫顏色值：

| 語意 | Tailwind class |
|---|---|
| 主要文字 | `text-foreground` |
| 次要文字 | `text-muted-foreground` |
| 主色背景 | `bg-primary` / `text-primary-foreground` |
| 錯誤 | `text-destructive` / `bg-destructive` |
| 邊框 | `border-border` |
| 背景 | `bg-background` / `bg-card` |

深色模式透過 `<html class="dark">` 切換，Token 會自動對應，**不要寫死顏色**（如 `text-gray-500`）。

---

## 六、className 合併

使用 `cn()` 工具函式進行 className 合併（已在 `@/lib/utils` 匯出）：

```tsx
import { cn } from '@/lib/utils'

<div className={cn('base-class', isActive && 'active-class', className)} />
```

不得使用字串拼接（template literal）或直接疊加 className prop。
