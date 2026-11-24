# ADR 0010: Unsupervised per-family Isolation Forests plus rules (hybrid)

Status: accepted · Phase: Phase 5E/6B

## Context

No labelled real data exists. The v1 single Isolation Forest never flagged rapid bursts
(recall 0.0): one extreme feature out of six barely shortens an isolation path.

## Decision

v2 adds velocity features relative to each account's own cadence (no future data), and
fits one forest per feature family (amount, velocity), combining them by the maximum
training percentile. The deployed decision is model OR rule baseline, adopted because it
won on VALIDATION data; thresholds are never tuned on test worlds.

## Consequences

+ Burst recall ≈ 1.0 on unseen accounts; 0.88 when bursts slow down, where a fixed rule
catches 0.30.
+ Versioned, digest-verified, reproducible artifacts; drift monitoring (PSI).
- The model alone has ~1.2 % false positives; the rules are near an upper bound because they
restate the synthetic generator. Nothing here says anything about real fraud.

## Alternatives considered

Supervised classifier (no real labels; would learn the generator); one larger forest
(dilution, measured).
