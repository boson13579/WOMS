/**
 * Pagination controls for the audit events table.
 *
 * Buttons: [<< First] [< Prev] [Next >] [Last >>] with page-size select.
 * State lives in URL search params, owned by `AuditPage`; this component
 * is purely presentational and bubbles changes via callbacks.
 *
 * Boundary handling: First / Prev disabled on page 1, Next / Last
 * disabled on the last page (or always disabled when `totalPages <= 0`).
 * Page-size change resets the caller to page 1 — owner enforces this
 * via the `onPageSizeChange` handler.
 */
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Select } from '@/components/ui/select';

export const AUDIT_PAGE_SIZE_OPTIONS = [20, 50, 100] as const;
export type AuditPageSize = (typeof AUDIT_PAGE_SIZE_OPTIONS)[number];

interface AuditPaginationProps {
  page: number;
  pageSize: AuditPageSize;
  total: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: AuditPageSize) => void;
  disabled?: boolean;
}

export function AuditPagination({
  page,
  pageSize,
  total,
  onPageChange,
  onPageSizeChange,
  disabled = false,
}: Readonly<AuditPaginationProps>): JSX.Element {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const isFirstPage = page <= 1;
  const isLastPage = page >= totalPages;

  return (
    <div className="flex flex-col gap-3 border-t border-border px-4 py-3 text-sm md:flex-row md:items-center md:justify-between">
      <div className="text-muted-foreground">
        Page {page} of {totalPages}
        <span className="mx-2">·</span>
        {total} total event{total === 1 ? '' : 's'}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          aria-label="First page"
          disabled={disabled || isFirstPage}
          onClick={() => {
            onPageChange(1);
          }}
        >
          {'<<'} First
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          aria-label="Previous page"
          disabled={disabled || isFirstPage}
          onClick={() => {
            onPageChange(page - 1);
          }}
        >
          {'<'} Prev
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          aria-label="Next page"
          disabled={disabled || isLastPage}
          onClick={() => {
            onPageChange(page + 1);
          }}
        >
          Next {'>'}
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          aria-label="Last page"
          disabled={disabled || isLastPage}
          onClick={() => {
            onPageChange(totalPages);
          }}
        >
          Last {'>>'}
        </Button>

        <div className="ml-2 flex items-center gap-1.5">
          <Label htmlFor="audit-page-size" className="text-xs text-muted-foreground">
            Page size
          </Label>
          <Select
            id="audit-page-size"
            value={String(pageSize)}
            className="w-20"
            disabled={disabled}
            onChange={(event) => {
              const next = Number(event.target.value);
              if (AUDIT_PAGE_SIZE_OPTIONS.includes(next as AuditPageSize)) {
                onPageSizeChange(next as AuditPageSize);
              }
            }}
            aria-label="Page size"
          >
            {AUDIT_PAGE_SIZE_OPTIONS.map((size) => (
              <option key={size} value={size}>
                {size}
              </option>
            ))}
          </Select>
        </div>
      </div>
    </div>
  );
}
