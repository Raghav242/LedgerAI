import type { ReactNode } from "react";

/** aria-describedby value matching the ids FormField renders. */
export function describedBy(id: string, error?: string, hint?: ReactNode): string | undefined {
  if (error) return `${id}-error`;
  if (hint) return `${id}-hint`;
  return undefined;
}
