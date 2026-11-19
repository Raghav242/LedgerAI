import { AlertTriangle, Inbox, Loader2, RefreshCw, WifiOff } from "lucide-react";
import type { ReactNode } from "react";
import { isApiError } from "@/api/client";
import { describeError } from "@/lib/errors";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/misc";
import { cn } from "@/lib/utils";

export function EmptyState({
  title,
  description,
  action,
  icon,
  className,
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  icon?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-center justify-center gap-2 px-4 py-10 text-center", className)}>
      <div className="mb-1 rounded-full border bg-muted/40 p-2.5 text-muted-foreground">{icon ?? <Inbox className="size-5" />}</div>
      <p className="text-sm font-medium">{title}</p>
      {description && <p className="max-w-sm text-sm text-muted-foreground">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function ErrorState({
  error,
  onRetry,
  retrying,
  className,
  compact,
}: {
  error: unknown;
  onRetry?: () => void;
  retrying?: boolean;
  className?: string;
  compact?: boolean;
}) {
  const info = describeError(error);
  const offline = isApiError(error) && (error.kind === "network" || error.kind === "timeout");
  return (
    <div
      role="alert"
      className={cn(
        "flex flex-col items-center justify-center gap-2 text-center",
        compact ? "px-3 py-4" : "px-4 py-10",
        className,
      )}
    >
      <div className="rounded-full border border-destructive/30 bg-destructive/10 p-2.5 text-destructive">
        {offline ? <WifiOff className="size-5" /> : <AlertTriangle className="size-5" />}
      </div>
      <p className="text-sm font-medium">{info.title}</p>
      <p className="max-w-md text-sm text-muted-foreground">{info.message}</p>
      {info.code && <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">{info.code}</code>}
      {onRetry && (
        <Button variant="outline" size="sm" className="mt-1" onClick={onRetry} disabled={retrying}>
          {retrying ? <Loader2 className="animate-spin" /> : <RefreshCw />}
          Try again
        </Button>
      )}
    </div>
  );
}

export function LoadingRows({ rows = 5, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn("flex flex-col gap-2 p-1", className)} role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-9 w-full" />
      ))}
      <span className="sr-only">Loading…</span>
    </div>
  );
}

export function InlineSpinner({ label = "Loading" }: { label?: string }) {
  return (
    <span role="status" className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
      <Loader2 className="size-3.5 animate-spin" />
      {label}
    </span>
  );
}
