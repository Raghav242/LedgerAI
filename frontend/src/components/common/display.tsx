import { Check, Copy, FlaskConical, Radio } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { shortId } from "@/lib/format";
import { formatMinor } from "@/lib/money";
import { cn } from "@/lib/utils";
import { TRANSFER_KIND_LABELS, type TransferKind } from "@/types/api";

export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0 space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="max-w-3xl text-sm text-muted-foreground">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

/** Amount in USD from integer cents. `signed` adds +/- and colours credits/debits. */
export function Money({ minor, signed, className }: { minor: number; signed?: boolean; className?: string }) {
  return (
    <span
      className={cn(
        "tabular font-mono",
        signed && minor > 0 && "text-success",
        signed && minor < 0 && "text-destructive",
        className,
      )}
    >
      {formatMinor(minor, { signed })}
    </span>
  );
}

export function CopyButton({ value, label = "Copy", className }: { value: string; label?: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  return (
    <Button
      variant="ghost"
      size="icon-sm"
      className={cn("size-6 text-muted-foreground", className)}
      aria-label={copied ? "Copied" : label}
      onClick={(event) => {
        event.stopPropagation();
        void navigator.clipboard?.writeText(value).then(() => {
          setCopied(true);
          clearTimeout(timer.current);
          timer.current = setTimeout(() => setCopied(false), 1500);
        });
      }}
    >
      {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
    </Button>
  );
}

/** Shortened UUID with the full value on hover and a copy button. */
export function IdText({ id, copy = true, className }: { id: string; copy?: boolean; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-0.5", className)}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="cursor-default font-mono text-xs text-muted-foreground" tabIndex={0}>
            {shortId(id)}
          </span>
        </TooltipTrigger>
        <TooltipContent className="font-mono">{id}</TooltipContent>
      </Tooltip>
      {copy && <CopyButton value={id} label="Copy id" />}
    </span>
  );
}

export function KindBadge({ kind }: { kind: TransferKind }) {
  const variant = kind === "transfer" ? "info" : kind === "dev_funding" ? "warning" : "muted";
  return <Badge variant={variant}>{TRANSFER_KIND_LABELS[kind] ?? kind}</Badge>;
}

export function StatusBadge({ status }: { status: string }) {
  return (
    <Badge variant={status === "completed" ? "success" : "secondary"} className="capitalize">
      {status}
    </Badge>
  );
}

/** Marks data provenance so real observations are never confused with teaching examples. */
export function DataSourceTag({ kind }: { kind: "live" | "example" }) {
  return kind === "live" ? (
    <Badge variant="success" title="Read from the running backend">
      <Radio /> Live data
    </Badge>
  ) : (
    <Badge variant="outline" className="border-dashed text-muted-foreground" title="Static illustration, not system data">
      <FlaskConical /> Illustrative example
    </Badge>
  );
}

export function KeyValue({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-1", className)}>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-sm">{children}</dd>
    </div>
  );
}
