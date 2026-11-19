import type { RouteObject } from "react-router";
import { NotFound } from "@/components/common/NotFound";
import { AppShell } from "@/components/layout/AppShell";
import { HistoryPage } from "@/features/history/HistoryPage";
import { LedgerPage } from "@/features/ledger/LedgerPage";
import { OverviewPage } from "@/features/overview/OverviewPage";
import { AnomalyLabPage, ExperimentLabPage, PerformanceLabPage, TopologyPage } from "@/features/planned/PlannedPages";
import { PlaygroundPage } from "@/features/transfers/PlaygroundPage";

export const routes: RouteObject[] = [
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <OverviewPage /> },
      { path: "playground", element: <PlaygroundPage /> },
      { path: "history", element: <HistoryPage /> },
      { path: "ledger", element: <LedgerPage /> },
      { path: "experiments", element: <ExperimentLabPage /> },
      { path: "topology", element: <TopologyPage /> },
      { path: "anomalies", element: <AnomalyLabPage /> },
      { path: "performance", element: <PerformanceLabPage /> },
      { path: "*", element: <NotFound /> },
    ],
  },
];
