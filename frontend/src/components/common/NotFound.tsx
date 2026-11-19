import { Link } from "react-router";
import { Button } from "@/components/ui/button";
import { EmptyState } from "./states";

export function NotFound() {
  return (
    <EmptyState
      title="Page not found"
      description="This page does not exist."
      action={
        <Button asChild size="sm">
          <Link to="/">Back to overview</Link>
        </Button>
      }
    />
  );
}
