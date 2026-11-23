import type { RouteObject } from "react-router";
import { NotFound } from "@/components/common/NotFound";
import { AppShell } from "@/components/layout/AppShell";
import { HistoryPage } from "@/features/history/HistoryPage";
import { LedgerPage } from "@/features/ledger/LedgerPage";
import { OverviewPage } from "@/features/overview/OverviewPage";
import { PlaygroundPage } from "@/features/transfers/PlaygroundPage";
import {
  AnomalyLabPage,
  EventPipelinePage,
  ExperimentLabPage,
  GuidedDemoPage,
  PerformanceLabPage,
  TopologyPage,
} from "./lazyPages";

/*
 * The everyday pages load with the app. The labs (larger, and the Performance Lab pulls in
 * the charting library) are separate chunks, loaded on first visit inside the shell's
 * <Suspense> boundary, so navigation stays visible while they load.
 */

export const routes: RouteObject[] = [
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <OverviewPage /> },
      { path: "playground", element: <PlaygroundPage /> },
      { path: "history", element: <HistoryPage /> },
      { path: "ledger", element: <LedgerPage /> },
      { path: "demo", element: <GuidedDemoPage /> },
      { path: "experiments", element: <ExperimentLabPage /> },
      { path: "topology", element: <TopologyPage /> },
      { path: "events", element: <EventPipelinePage /> },
      { path: "anomalies", element: <AnomalyLabPage /> },
      { path: "performance", element: <PerformanceLabPage /> },
      { path: "*", element: <NotFound /> },
    ],
  },
];
