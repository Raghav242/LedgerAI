import { Info, Loader2, Play, RotateCcw, ShieldAlert } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Disclosure } from "@/components/common/Disclosure";
import { FormField } from "@/components/common/FormField";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
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
  /** Controller-only experiments: whether the local controller can run it right now. */
  availability?: { runnable: boolean; reason: string | null };
}

/** What an infrastructure experiment will do, shown in the confirmation dialog. */
const DISTURBS: Record<string, string> = {
  redis_outage: "stop the local Redis container, then start it again",
  rabbitmq_outage: "stop the local RabbitMQ container, then start it again",
  worker_crash: "stop the event consumer and restart it twice with a crash hook, then restore it",
  replica_failure: "stop the api-2 container, then start it again",
};

/**
 * Configure and run one experiment: title, one-sentence purpose, the essential parameters
 * and the primary action. Expected behaviour, steps and parameter explanations live in a
 * collapsed "How this works".
 */
export function ExperimentDetail({ experiment, onRun, starting, blockedReason, availability }: ExperimentDetailProps) {
  // Keyed by experiment id in the parent, so switching experiments resets the form.
  const [raw, setRaw] = useState<RawParams>(() => defaultRawParams(experiment.params));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<Record<string, number> | null>(null);
  const copy = EXPERIMENT_COPY[experiment.id];
  const infrastructure = experiment.risk === "infrastructure";
  const runnable = infrastructure ? !!availability?.runnable : experiment.available;
  const unavailable = infrastructure ? availability?.reason : experiment.available ? null : experiment.unavailable_reason;

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    const result = validateParams(experiment.params, raw);
    if (!result.ok) {
      setErrors(result.errors);
      return;
    }
    setErrors({});
    // Infrastructure experiments never start without an explicit confirmation.
    if (infrastructure) setPending(result.params);
    else onRun(result.params);
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
            <Button type="submit" disabled={starting || !!blockedReason || !runnable}>
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
          {unavailable && (
            <p className="flex items-start gap-1.5 text-xs text-muted-foreground" role="status" data-testid="unavailable-reason">
              <Info className="mt-0.5 size-3.5 shrink-0" /> <span className="break-words">{unavailable}</span>
            </p>
          )}
          {blockedReason && (
            <p className="flex items-start gap-1.5 text-xs text-muted-foreground" role="status">
              <Info className="mt-0.5 size-3.5 shrink-0" /> {blockedReason}
            </p>
          )}
        </form>

        <Dialog open={pending !== null} onOpenChange={(open) => !open && setPending(null)}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2">
                <ShieldAlert className="size-4 text-warning" /> Disturb the local stack?
              </DialogTitle>
              <DialogDescription>
                This run will {DISTURBS[experiment.id] ?? "disturb one local service"}. It only touches this machine's Docker Compose stack, takes about{" "}
                {experiment.estimated_seconds} s, restores the service automatically and verifies recovery. Other experiments cannot run meanwhile.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="ghost" onClick={() => setPending(null)}>
                Cancel
              </Button>
              <Button
                onClick={() => {
                  if (pending) onRun(pending);
                  setPending(null);
                }}
              >
                <Play /> Run on my local stack
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>

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
