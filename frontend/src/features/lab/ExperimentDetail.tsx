import { Info, Loader2, Play, RotateCcw } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Disclosure } from "@/components/common/Disclosure";
import { FormField } from "@/components/common/FormField";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { describedBy } from "@/lib/a11y";
import { cn } from "@/lib/utils";
import type { ExperimentDefinition } from "@/types/api";
import { EXPERIMENT_COPY } from "./experimentIcons";
import { defaultRawParams, formatParam, SHORT_LABEL, validateParams, type RawParams } from "./params";

export interface ExperimentDetailProps {
  experiment: ExperimentDefinition;
  onRun: (params: Record<string, number>) => void;
  starting: boolean;
  /** Another run is active: running is disabled and the reason shown. */
  blockedReason?: string | null;
}

/**
 * Configure and run one experiment: title, one-sentence purpose, the essential parameters
 * and the primary action. Expected behaviour, steps and parameter explanations live in a
 * collapsed "How this works".
 */
export function ExperimentDetail({ experiment, onRun, starting, blockedReason }: ExperimentDetailProps) {
  // Keyed by experiment id in the parent, so switching experiments resets the form.
  const [raw, setRaw] = useState<RawParams>(() => defaultRawParams(experiment.params));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const copy = EXPERIMENT_COPY[experiment.id];

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    const result = validateParams(experiment.params, raw);
    if (!result.ok) {
      setErrors(result.errors);
      return;
    }
    setErrors({});
    onRun(result.params);
  }

  return (
    <Card className="gap-4 py-4">
      <CardContent className="space-y-4 px-4">
        <div>
          <h2 className="text-base font-semibold">{experiment.title}</h2>
          <p className="text-sm text-muted-foreground">{copy.question}</p>
        </div>

        <form onSubmit={onSubmit} noValidate aria-label={`${experiment.title} parameters`} className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-1">
            {experiment.params.map((spec) => {
              const id = `param-${experiment.id}-${spec.name}`;
              const money = spec.unit === "cents";
              const hint = `${formatParam(spec, spec.minimum)}–${formatParam(spec, spec.maximum)}`;
              return (
                <FormField key={spec.name} id={id} label={SHORT_LABEL[spec.name] ?? spec.label} error={errors[spec.name]} hint={hint}>
                  <div className="relative">
                    {money && <span className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-sm text-muted-foreground">$</span>}
                    <Input
                      id={id}
                      name={spec.name}
                      inputMode={money ? "decimal" : "numeric"}
                      value={raw[spec.name] ?? ""}
                      onChange={(e) => setRaw((r) => ({ ...r, [spec.name]: e.target.value }))}
                      aria-invalid={!!errors[spec.name]}
                      aria-describedby={describedBy(id, errors[spec.name], hint)}
                      className={cn("font-mono", money && "pl-7", spec.unit === "seconds" && "pr-8")}
                    />
                    {spec.unit === "seconds" && (
                      <span className="pointer-events-none absolute top-1/2 right-3 -translate-y-1/2 text-sm text-muted-foreground">s</span>
                    )}
                  </div>
                </FormField>
              );
            })}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button type="submit" disabled={starting || !!blockedReason || !experiment.available}>
              {starting ? <Loader2 className="animate-spin" /> : <Play />}
              {starting ? "Starting…" : "Run experiment"}
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => {
                setRaw(defaultRawParams(experiment.params));
                setErrors({});
              }}
            >
              <RotateCcw /> Defaults
            </Button>
          </div>
          {blockedReason && (
            <p className="flex items-start gap-1.5 text-xs text-muted-foreground" role="status">
              <Info className="mt-0.5 size-3.5 shrink-0" /> {blockedReason}
            </p>
          )}
        </form>

        <Disclosure title="How this works">
          <div className="space-y-4 text-sm">
            <p>
              <span className="font-medium">Concept: </span>
              <span className="text-muted-foreground">{experiment.concept}</span>
            </p>
            <section>
              <h3 className="mb-1.5 text-xs font-medium text-muted-foreground">Expected behaviour</h3>
              <ul className="list-disc space-y-1 pl-5">
                {experiment.expected.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </section>
            <section>
              <h3 className="mb-1.5 text-xs font-medium text-muted-foreground">What the run does</h3>
              <ol className="list-decimal space-y-1 pl-5 text-muted-foreground">
                {experiment.how_it_works.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ol>
            </section>
            <section>
              <h3 className="mb-1.5 text-xs font-medium text-muted-foreground">Parameters</h3>
              <dl className="space-y-1.5">
                {experiment.params.map((spec) => (
                  <div key={spec.name}>
                    <dt className="inline font-medium">{spec.label}: </dt>
                    <dd className="inline text-muted-foreground">
                      {spec.description} Default {formatParam(spec, spec.default)}.
                    </dd>
                  </div>
                ))}
              </dl>
            </section>
            <p className="text-xs text-muted-foreground">{experiment.risk_note}</p>
          </div>
        </Disclosure>
      </CardContent>
    </Card>
  );
}
