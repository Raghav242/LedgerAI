import { Lightbulb } from "lucide-react";
import type { ReactNode } from "react";
import { Disclosure } from "@/components/common/Disclosure";
import type { ExperimentDefinition, LabRun } from "@/types/api";
import { EXPERIMENT_COPY } from "./experimentIcons";

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-1">
      <h4 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">{title}</h4>
      <div className="text-sm">{children}</div>
    </section>
  );
}

/**
 * The educational content of a run in one consistent shape: what we test, what happened
 * (from the actual result), why it works, and where to look in the implementation.
 * Collapsed after a passing run; open when something failed.
 */
export function GuidedExplanation({ run, experiment }: { run: LabRun; experiment?: ExperimentDefinition }) {
  if (!run.result) return null;
  return (
    <Disclosure variant="boxed" icon={<Lightbulb />} title="Understand this result" defaultOpen={run.status !== "passed"}>
      <div className="space-y-4" data-testid="guided-explanation">
        <Section title="What are we testing?">
          {EXPERIMENT_COPY[run.experiment].question}
          {experiment && <span className="text-muted-foreground"> ({experiment.concept})</span>}
        </Section>
        <Section title="What happened?">{run.result.summary}</Section>
        <Section title="Why does it work?">
          <ul className="list-disc space-y-1.5 pl-5">
            {run.result.explanation.map((line, i) => (
              <li key={i}>{line}</li>
            ))}
          </ul>
        </Section>
        <Section title="Explore the implementation">
          {experiment && (
            <ol className="mb-2 list-decimal space-y-1 pl-5 text-muted-foreground">
              {experiment.how_it_works.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ol>
          )}
          <p className="text-xs text-muted-foreground">
            Source: <code className="font-mono">app/lab/experiments/{run.experiment}.py</code> · design notes:{" "}
            <code className="font-mono">docs/phase-5b-design.md</code>
          </p>
        </Section>
      </div>
    </Disclosure>
  );
}
