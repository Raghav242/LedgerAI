# Phase 5F: Final UX, guided demo and reports

## 1. Principle

Every page answers in this order: **what is this → what can I do → what happened → did it
pass → what next**. Metrics, invariants, timelines, component details, implementation
decisions and raw data are one click away (tabs, `Disclosure`, drawers). They are never
on screen all at once. The Phase 5B redesign (`docs/phase-5b-ux-redesign.md`) introduced
this for the Experiment Lab; 5F applies it to every new page.

| Page | Default view | On demand |
|---|---|---|
| Overview | live numbers, reconciliation, a "Start guided demo" entry | recent activity |
| Guided demo | one step: why, safe defaults, expected, last recorded result, a link | the other six steps |
| Experiment Lab | compact cards in two groups (correctness / failure & recovery); result = status + one sentence + 4 numbers + one visual | Request flow · Checks · Timeline · Technical details tabs; "Understand this result"; JSON export |
| System Topology | verdict line + architecture graph with compact nodes (name, status dot, one number) | details panel per node (evidence, last OK, error, metrics, design decisions); illustrated flows |
| Event Pipeline | verdict + four stage tiles + recent events | retries, queues, publish lag; per-event lifecycle drawer |
| AI Anomaly Lab | scenario cards, verdict, score vs threshold, three plain-language reasons | feature table vs training range, event path, system checks, model card, recent results |
| Performance Lab | throughput chart, per-workload table, "What did we learn?" | raw configuration and statistics; new benchmark run |

Visual rules that held throughout: status is shown as icon + word, never colour alone.
A badge appears only where it carries information ("Recorded results, not live",
"Matches/Differs from the scenario's intent"). Animation is limited to progress and a
pulse on pending stages, both disabled under `prefers-reduced-motion`.

## 2. Guided demo (`/demo`)

Seven steps: a basic transfer, concurrent withdrawals, idempotent retries, distributed
rate limiting, RabbitMQ outage and recovery, AI anomaly detection, performance
comparison. Each step has a short explanation, safe default parameters, the expected
behaviour, a link to the relevant lab and the **actual observed result**. That result is
the most recent *recorded* run, transfer or benchmark, labelled "previously recorded,
not run by this guide", or "Not run recently".

There is no tutorial engine. The step lives in the URL (`?step=3`). The guide never
starts anything: a test asserts that it issues only GET requests. The failure step is
locked, with the reason shown, unless a local controller with `--enable-failures` is
reachable. In a public or unsafe environment the controller cannot be reached, so the
step can never be run there.

## 3. Reusable experiment reports

Every finished run has a **JSON** button (`features/lab/report.ts`). The report contains:
experiment and run id, configuration, timestamps, outcome, environment (runner, target,
replicas actually observed), and two clearly separated sections:

- `observed`: summary, error, metrics, outcome counts, invariant checks, result data,
  important ids (transfer/event ids from the result, a sample of request ids) and timeline
  counts
- `explanatory`: catalog concept, expected behaviour and explanation text, marked "Not an
  observation"

## 4. Frontend quality

- **Route splitting.** Overview, Playground, History and Ledger load with the app; the
  labs are `lazy` routes. Main bundle: 1,269 kB → 585 kB. Recharts (415 kB) loads only
  with a chart.
- **Responsive.** Playwright checks that pages have no horizontal overflow at 1024, 768
  and 390 px (and runs at 1440). The topology graph becomes grouped lists below 768 px.
- **Accessible.** All controls are native buttons, links, inputs and selects. Scenario
  pickers are radio groups. Each chart has a text alternative listing its values.
  Status changes are announced (`aria-live`).
- **Charts.** Series colours come from the design tokens; the second series token
  (`--chart-3`) was chosen with the dataviz palette validator (CVD and normal-vision
  separation, contrast) in both themes. Charts use one axis, a legend plus selective
  direct labels, and a tooltip.
- **Honest states.** Loading, empty, disabled-backend and error states exist on every
  new page. Stale health is shown as stale. Recorded benchmarks are labelled as recorded.
- The placeholder pages and the "Roadmap" nav section are gone, because nothing is
  planned any more.
