import { ChevronRight } from "lucide-react";
import { useId, useState, type ReactNode } from "react";
import { cn } from "@/lib/utils";

/**
 * Progressive disclosure: a labelled button that shows or hides secondary content.
 * Used for "How this works", technical details, invariant lists, request metadata, ...
 * Content is not rendered while closed, so the page stays light; nothing is lost.
 */
export function Disclosure({
  title,
  meta,
  icon,
  defaultOpen = false,
  open: controlledOpen,
  onOpenChange,
  variant = "plain",
  className,
  children,
}: {
  title: ReactNode;
  /** Short trailing text (a count, a status). */
  meta?: ReactNode;
  icon?: ReactNode;
  defaultOpen?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** "plain": inline row; "boxed": its own bordered section. */
  variant?: "plain" | "boxed";
  className?: string;
  children: ReactNode;
}) {
  const [uncontrolled, setUncontrolled] = useState(defaultOpen);
  const open = controlledOpen ?? uncontrolled;
  const id = useId();
  const toggle = () => {
    setUncontrolled(!open);
    onOpenChange?.(!open);
  };
  return (
    <div className={cn(variant === "boxed" && "rounded-lg border", className)} data-open={open}>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        onClick={toggle}
        className={cn(
          "flex w-full items-center gap-2 rounded-md text-left text-sm font-medium outline-none hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50",
          variant === "boxed" ? "px-4 py-3" : "py-1.5 text-muted-foreground",
        )}
      >
        <ChevronRight className={cn("size-4 shrink-0 transition-transform motion-reduce:transition-none", open && "rotate-90")} aria-hidden />
        {icon && <span className="text-muted-foreground [&>svg]:size-4">{icon}</span>}
        <span className={cn("min-w-0 flex-1", variant === "boxed" && "text-foreground")}>{title}</span>
        {meta && <span className="shrink-0 text-xs font-normal text-muted-foreground">{meta}</span>}
      </button>
      {open && (
        <div id={id} className={cn(variant === "boxed" ? "border-t px-4 py-4" : "pt-2 pb-1")}>
          {children}
        </div>
      )}
    </div>
  );
}
