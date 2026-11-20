import { Badge } from "@/components/ui/badge";
import type { LabRunStatus } from "@/types/api";
import { RUN_STATUS } from "./runStatus";

export function RunStatusBadge({ status }: { status: LabRunStatus }) {
  const meta = RUN_STATUS[status];
  return (
    <Badge variant={meta.variant} data-testid="run-status" data-status={status} title={meta.description}>
      {meta.icon}
      {meta.label}
    </Badge>
  );
}
