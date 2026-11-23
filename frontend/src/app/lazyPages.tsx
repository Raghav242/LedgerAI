/** Code-split lab pages (see routes.tsx). */
import { lazy } from "react";

export const GuidedDemoPage = lazy(() => import("@/features/demo/GuidedDemoPage").then((m) => ({ default: m.GuidedDemoPage })));
export const ExperimentLabPage = lazy(() => import("@/features/lab/ExperimentLabPage").then((m) => ({ default: m.ExperimentLabPage })));
export const TopologyPage = lazy(() => import("@/features/topology/TopologyPage").then((m) => ({ default: m.TopologyPage })));
export const EventPipelinePage = lazy(() => import("@/features/events/EventPipelinePage").then((m) => ({ default: m.EventPipelinePage })));
export const AnomalyLabPage = lazy(() => import("@/features/ai/AnomalyLabPage").then((m) => ({ default: m.AnomalyLabPage })));
export const PerformanceLabPage = lazy(() => import("@/features/performance/PerformanceLabPage").then((m) => ({ default: m.PerformanceLabPage })));
