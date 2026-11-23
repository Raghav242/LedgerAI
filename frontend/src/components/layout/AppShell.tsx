import { Menu, Moon, Sun } from "lucide-react";
import { Suspense, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router";
import { LoadingRows } from "@/components/common/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/dialog";
import { useTheme } from "@/hooks/useTheme";
import { cn } from "@/lib/utils";
import { ConnectionStatus } from "./ConnectionStatus";
import { NAV_ITEMS, type NavItem } from "./nav";

function Brand() {
  return (
    <div className="flex items-center gap-2.5 px-2">
      <img src="/favicon.svg" alt="" className="size-7 rounded-md" />
      <div className="leading-tight">
        <div className="text-sm font-semibold tracking-tight">LedgerFlow AI</div>
        <div className="text-[11px] text-muted-foreground">Distributed Systems Lab</div>
      </div>
    </div>
  );
}

function NavEntry({ item, onNavigate }: { item: NavItem; onNavigate?: () => void }) {
  const Icon = item.icon;
  return (
    <NavLink
      to={item.to}
      end={item.to === "/"}
      onClick={onNavigate}
      title={item.description}
      className={({ isActive }) =>
        cn(
          "group flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50",
          isActive ? "bg-accent font-medium text-foreground" : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
        )
      }
    >
      <Icon className="size-4 shrink-0" aria-hidden />
      <span className="flex-1 truncate">{item.label}</span>
      {item.status === "planned" && (
        <Badge variant="muted" className="px-1.5 py-0 text-[10px] font-normal">
          Planned
        </Badge>
      )}
    </NavLink>
  );
}

function SidebarNav({ onNavigate }: { onNavigate?: () => void }) {
  const live = NAV_ITEMS.filter((i) => i.status === "live");
  const planned = NAV_ITEMS.filter((i) => i.status === "planned");
  return (
    <nav aria-label="Main" className="flex flex-1 flex-col gap-6">
      <div className="flex flex-col gap-0.5">
        <div className="px-2.5 pb-1.5 text-[11px] font-medium tracking-wide text-muted-foreground/80 uppercase">Lab</div>
        {live.map((item) => (
          <NavEntry key={item.to} item={item} onNavigate={onNavigate} />
        ))}
      </div>
      {planned.length > 0 && (
        <div className="flex flex-col gap-0.5">
          <div className="px-2.5 pb-1.5 text-[11px] font-medium tracking-wide text-muted-foreground/80 uppercase">Roadmap</div>
          {planned.map((item) => (
            <NavEntry key={item.to} item={item} onNavigate={onNavigate} />
          ))}
        </div>
      )}
    </nav>
  );
}

function SimulatedMoneyNote() {
  return (
    <div className="rounded-lg border border-warning/30 bg-warning/5 p-3 text-xs text-muted-foreground">
      <div className="mb-0.5 font-medium text-warning">Simulated money only</div>
      All balances are virtual USD for experimentation. No real payments, cards or bank details.
    </div>
  );
}

export function AppShell() {
  const { theme, toggle } = useTheme();
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();
  const current = NAV_ITEMS.find((i) => (i.to === "/" ? location.pathname === "/" : location.pathname.startsWith(i.to)));

  return (
    <div className="flex min-h-svh">
      <aside className="sticky top-0 hidden h-svh w-64 shrink-0 flex-col gap-6 border-r bg-sidebar p-4 lg:flex">
        <Brand />
        <SidebarNav />
        <SimulatedMoneyNote />
      </aside>

      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent side="left" className="gap-6 bg-sidebar p-4">
          <SheetTitle className="sr-only">Navigation</SheetTitle>
          <SheetDescription className="sr-only">Main navigation</SheetDescription>
          <Brand />
          <SidebarNav onNavigate={() => setMobileOpen(false)} />
          <SimulatedMoneyNote />
        </SheetContent>
      </Sheet>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b bg-background/85 px-4 backdrop-blur sm:px-6">
          <Button variant="ghost" size="icon-sm" className="lg:hidden" aria-label="Open navigation" onClick={() => setMobileOpen(true)}>
            <Menu />
          </Button>
          <div className="min-w-0 flex-1 truncate text-sm font-medium">
            {current?.label ?? "LedgerFlow AI"}
            {current?.status === "planned" && <span className="ml-2 text-xs font-normal text-muted-foreground">· planned (Phase {current.phase})</span>}
          </div>
          <Badge variant="warning" className="hidden sm:inline-flex">
            Virtual USD
          </Badge>
          <ConnectionStatus />
          <Button variant="ghost" size="icon-sm" onClick={toggle} aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}>
            {theme === "dark" ? <Sun /> : <Moon />}
          </Button>
        </header>
        <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 sm:px-6">
          {/* Lab pages are code-split; the shell stays put while one loads. */}
          <Suspense fallback={<LoadingRows rows={4} />}>
            <Outlet />
          </Suspense>
        </main>
      </div>
    </div>
  );
}
