import { ArrowDown, ArrowUp } from "lucide-react";
import { IdText, KindBadge, Money, StatusBadge } from "@/components/common/display";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatDateTime, formatShortDateTime } from "@/lib/format";
import type { Transfer } from "@/types/api";

export type SortKey = "created_at" | "amount_minor";
export interface SortState {
  key: SortKey;
  dir: "asc" | "desc";
}

function SortHeader({
  label,
  active,
  dir,
  onClick,
  align,
}: {
  label: string;
  active: boolean;
  dir: "asc" | "desc";
  onClick: () => void;
  align?: "right";
}) {
  return (
    <TableHead className={align === "right" ? "text-right" : undefined} aria-sort={active ? (dir === "asc" ? "ascending" : "descending") : "none"}>
      <button type="button" onClick={onClick} className="inline-flex items-center gap-1 hover:text-foreground">
        {label}
        {active && (dir === "asc" ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />)}
      </button>
    </TableHead>
  );
}

export function TransferTable({
  rows,
  sort,
  onSort,
  nameOf,
  onOpen,
  compact = false,
}: {
  rows: Transfer[];
  /** Hide the id and status columns (dashboards with little width). */
  compact?: boolean;
  sort?: SortState;
  onSort?: (key: SortKey) => void;
  nameOf: (id: string) => string;
  onOpen: (id: string) => void;
}) {
  return (
    <Table>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          {!compact && <TableHead>Transaction</TableHead>}
          {sort && onSort ? (
            <SortHeader label="Date / time" active={sort.key === "created_at"} dir={sort.dir} onClick={() => onSort("created_at")} />
          ) : (
            <TableHead>Date / time</TableHead>
          )}
          <TableHead>Source</TableHead>
          <TableHead>Destination</TableHead>
          {sort && onSort ? (
            <SortHeader label="Amount" align="right" active={sort.key === "amount_minor"} dir={sort.dir} onClick={() => onSort("amount_minor")} />
          ) : (
            <TableHead className="text-right">Amount</TableHead>
          )}
          <TableHead>Type</TableHead>
          {!compact && <TableHead>Status</TableHead>}
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((t) => (
          <TableRow
            key={t.id}
            className="cursor-pointer"
            tabIndex={0}
            onClick={() => onOpen(t.id)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onOpen(t.id);
              }
            }}
            aria-label={`Open transfer ${t.id}`}
          >
            {!compact && (
              <TableCell>
                <IdText id={t.id} copy={false} />
              </TableCell>
            )}
            <TableCell className="text-muted-foreground" title={formatDateTime(t.created_at)}>
              {compact ? formatShortDateTime(t.created_at) : formatDateTime(t.created_at)}
            </TableCell>
            <TableCell className={compact ? "max-w-[9rem] truncate" : "max-w-[11rem] truncate"}>{nameOf(t.source_account_id)}</TableCell>
            <TableCell className={compact ? "max-w-[9rem] truncate" : "max-w-[11rem] truncate"}>{nameOf(t.destination_account_id)}</TableCell>
            <TableCell className="text-right">
              <Money minor={t.amount_minor} />
            </TableCell>
            <TableCell>
              <KindBadge kind={t.kind} />
            </TableCell>
            {!compact && (
              <TableCell>
                <StatusBadge status={t.status} />
              </TableCell>
            )}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
