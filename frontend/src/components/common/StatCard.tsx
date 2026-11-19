import type { ReactNode } from "react";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/misc";
import { cn } from "@/lib/utils";

export function StatCard({
  label,
  value,
  hint,
  icon,
  loading,
  error,
  className,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  icon?: ReactNode;
  loading?: boolean;
  error?: boolean;
  className?: string;
}) {
  return (
    <Card className={cn("gap-2 py-4", className)}>
      <div className="flex items-center justify-between px-4">
        <span className="text-xs font-medium text-muted-foreground">{label}</span>
        {icon && <span className="text-muted-foreground [&_svg]:size-4">{icon}</span>}
      </div>
      <div className="px-4">
        {loading ? (
          <Skeleton className="h-7 w-28" />
        ) : error ? (
          <span className="text-sm text-destructive">Unavailable</span>
        ) : (
          <div className="tabular text-2xl font-semibold tracking-tight">{value}</div>
        )}
      </div>
      {hint && <div className="px-4 text-xs text-muted-foreground">{hint}</div>}
    </Card>
  );
}
