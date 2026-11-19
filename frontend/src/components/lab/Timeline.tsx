import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/**
 * Vertical event timeline. Used for the transfer attempt log in Phase 5A; intended for
 * experiment runs (request fan-out, outages, recoveries) in Phase 5B.
 */
export type TimelineTone = "success" | "warning" | "destructive" | "info" | "muted";

export interface TimelineEvent {
  id: string;
  title: ReactNode;
  time?: ReactNode;
  tone: TimelineTone;
  body?: ReactNode;
}

const DOT: Record<TimelineTone, string> = {
  success: "bg-success",
  warning: "bg-warning",
  destructive: "bg-destructive",
  info: "bg-info",
  muted: "bg-muted-foreground/50",
};

export function Timeline({ events, className }: { events: TimelineEvent[]; className?: string }) {
  return (
    <ol className={cn("relative space-y-4 border-l pl-5", className)}>
      {events.map((event) => (
        <li key={event.id} className="relative">
          <span className={cn("absolute top-1.5 -left-[25px] size-2.5 rounded-full ring-4 ring-card", DOT[event.tone])} aria-hidden />
          <div className="flex flex-wrap items-baseline justify-between gap-x-3">
            <div className="text-sm font-medium">{event.title}</div>
            {event.time && <div className="font-mono text-xs text-muted-foreground">{event.time}</div>}
          </div>
          {event.body && <div className="mt-1 text-xs text-muted-foreground">{event.body}</div>}
        </li>
      ))}
    </ol>
  );
}
