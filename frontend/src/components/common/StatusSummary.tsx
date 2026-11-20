import { AlertTriangle, CheckCircle2, CircleDashed, Loader2, XCircle } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export type SummaryTone = "success" | "destructive" | "warning" | "info" | "muted";

const STYLE: Record<SummaryTone, { box: string; text: string; icon: ReactNode }> = {
  success: { box: "border-success/30 bg-success/5", text: "text-success", icon: <CheckCircle2 /> },
  destructive: { box: "border-destructive/40 bg-destructive/5", text: "text-destructive", icon: <XCircle /> },
  warning: { box: "border-warning/40 bg-warning/5", text: "text-warning", icon: <AlertTriangle /> },
  info: { box: "border-info/30 bg-info/5", text: "text-info", icon: <Loader2 className="motion-safe:animate-spin" /> },
  muted: { box: "bg-muted/20", text: "text-muted-foreground", icon: <CircleDashed /> },
};

/**
 * The one-line verdict a page leads with ("Ledger balanced", "Passed", ...). Icon + words,
 * never colour alone.
 */
export function StatusSummary({
  tone,
  title,
  children,
  actions,
  icon,
  className,
  ...rest
}: {
  tone: SummaryTone;
  title: ReactNode;
  children?: ReactNode;
  actions?: ReactNode;
  icon?: ReactNode;
  className?: string;
} & Record<`data-${string}`, string | undefined>) {
  const style = STYLE[tone];
  return (
    <div className={cn("flex flex-wrap items-start gap-3 rounded-lg border p-4", style.box, className)} {...rest}>
      <span className={cn("mt-0.5 [&>svg]:size-5", style.text)} aria-hidden>
        {icon ?? style.icon}
      </span>
      <div className="min-w-0 flex-1 space-y-0.5">
        <div className={cn("font-semibold", style.text)}>{title}</div>
        {children && <div className="text-sm text-muted-foreground">{children}</div>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

/** A small number with a label; the compact sibling of StatCard. */
export function CompactMetric({
  label,
  value,
  tone,
  hint,
  className,
  ...rest
}: {
  label: ReactNode;
  value: ReactNode;
  tone?: "success" | "warning" | "destructive" | "info";
  hint?: ReactNode;
  className?: string;
} & Record<`data-${string}`, string | undefined>) {
  return (
    <div className={cn("min-w-0 rounded-lg border bg-card px-3 py-2.5", className)} {...rest}>
      <div className="truncate text-xs text-muted-foreground">{label}</div>
      <div
        className={cn(
          "tabular text-xl font-semibold tracking-tight",
          tone === "success" && "text-success",
          tone === "warning" && "text-warning",
          tone === "destructive" && "text-destructive",
          tone === "info" && "text-info",
        )}
      >
        {value}
      </div>
      {hint && <div className="truncate text-[11px] text-muted-foreground">{hint}</div>}
    </div>
  );
}
