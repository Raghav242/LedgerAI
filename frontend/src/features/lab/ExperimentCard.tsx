import { cn } from "@/lib/utils";
import type { ExperimentDefinition } from "@/types/api";
import { EXPERIMENT_COPY, EXPERIMENT_ICON } from "./experimentIcons";

/** Compact, selectable experiment tile: icon, short name, one-line purpose. */
export function ExperimentCard({
  experiment,
  selected,
  onSelect,
}: {
  experiment: ExperimentDefinition;
  selected: boolean;
  onSelect: () => void;
}) {
  const Icon = EXPERIMENT_ICON[experiment.id];
  const copy = EXPERIMENT_COPY[experiment.id];
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      data-experiment={experiment.id}
      title={experiment.title}
      className={cn(
        "flex h-full w-full items-start gap-3 rounded-lg border bg-card px-3 py-3 text-left transition-colors outline-none hover:bg-accent/50 focus-visible:ring-[3px] focus-visible:ring-ring/50 motion-reduce:transition-none",
        selected && "border-primary/60 bg-accent/60",
      )}
    >
      <span
        className={cn(
          "flex size-8 shrink-0 items-center justify-center rounded-md border bg-muted/40 text-muted-foreground",
          selected && "border-transparent bg-primary text-primary-foreground",
        )}
      >
        <Icon className="size-4" aria-hidden />
      </span>
      <span className="min-w-0">
        <span className="block text-sm font-semibold">{copy.name}</span>
        <span className="block text-xs text-muted-foreground">{copy.tagline}</span>
      </span>
    </button>
  );
}
