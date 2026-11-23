import { CircleDashed } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ComponentStatus } from "@/types/api";
import { STATUS_META } from "./healthMeta";

export function StatusDot({ status, className }: { status: ComponentStatus; className?: string }) {
  return <span aria-hidden className={cn("inline-block size-2.5 shrink-0 rounded-full", STATUS_META[status].dot, className)} />;
}

export function StatusLabel({ status, className }: { status: ComponentStatus; className?: string }) {
  const meta = STATUS_META[status];
  const Icon = meta.icon ?? CircleDashed;
  return (
    <span className={cn("inline-flex items-center gap-1 text-xs font-medium", meta.text, className)}>
      <Icon className="size-3.5" aria-hidden /> {meta.label}
    </span>
  );
}

