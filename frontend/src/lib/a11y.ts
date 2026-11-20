import type { ReactNode } from "react";

/** aria-describedby value matching the ids FormField renders. */
export function describedBy(id: string, error?: string, hint?: ReactNode): string | undefined {
  if (error) return `${id}-error`;
  if (hint) return `${id}-hint`;
  return undefined;
}

/** Props for the panel controlled by a <Tabs> tab with the same idPrefix and id. */
export function tabPanelProps(idPrefix: string, id: string) {
  return { role: "tabpanel", id: `${idPrefix}-panel-${id}`, "aria-labelledby": `${idPrefix}-tab-${id}`, tabIndex: 0 } as const;
}
