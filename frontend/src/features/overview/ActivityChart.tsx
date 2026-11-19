import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis, type TooltipContentProps } from "recharts";
import type { NameType, ValueType } from "recharts/types/component/DefaultTooltipContent";
import { formatCount } from "@/lib/format";
import { formatMinor } from "@/lib/money";
import type { DailyTransferActivity } from "@/types/api";

const dayLabel = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
const fullDayLabel = new Intl.DateTimeFormat("en-US", { weekday: "short", month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });

function asUtcDate(day: string): Date {
  return new Date(`${day}T00:00:00Z`);
}

function ActivityTooltip({ active, payload }: TooltipContentProps<ValueType, NameType>) {
  const row = payload?.[0]?.payload as DailyTransferActivity | undefined;
  if (!active || !row) return null;
  return (
    <div className="rounded-md border bg-popover px-3 py-2 text-xs shadow-md">
      <div className="mb-1 font-medium">{fullDayLabel.format(asUtcDate(row.date))} (UTC)</div>
      <div className="flex items-center gap-2">
        <span className="size-2 rounded-sm bg-chart-1" aria-hidden />
        <span className="text-muted-foreground">Transfers</span>
        <span className="ml-auto font-mono tabular">{formatCount(row.count)}</span>
      </div>
      <div className="flex items-center gap-2 pl-4">
        <span className="text-muted-foreground">Volume</span>
        <span className="ml-auto font-mono tabular">{formatMinor(row.volume_minor)}</span>
      </div>
    </div>
  );
}

/** Merchant-to-merchant transfers per UTC day, from GET /api/v1/stats/summary. One series. */
export function ActivityChart({ data }: { data: DailyTransferActivity[] }) {
  return (
    <figure className="m-0">
      <div className="h-56 w-full" role="img" aria-label={`Bar chart of transfers per day over the last ${data.length} days`}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 8, right: 4, bottom: 0, left: -12 }} barCategoryGap={2}>
            <CartesianGrid vertical={false} stroke="var(--border)" />
            <XAxis
              dataKey="date"
              tickFormatter={(d: string) => dayLabel.format(asUtcDate(d))}
              tickLine={false}
              axisLine={false}
              tick={{ fill: "var(--muted-foreground)", fontSize: 11 }}
              interval="preserveStartEnd"
              minTickGap={16}
            />
            <YAxis
              allowDecimals={false}
              tickLine={false}
              axisLine={false}
              width={44}
              tick={{ fill: "var(--muted-foreground)", fontSize: 11 }}
            />
            <Tooltip content={ActivityTooltip} cursor={{ fill: "var(--muted)", opacity: 0.5 }} />
            <Bar dataKey="count" name="Transfers" fill="var(--chart-1)" radius={[4, 4, 0, 0]} maxBarSize={28} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <details className="mt-2 text-xs text-muted-foreground">
        <summary className="cursor-pointer">Show as table</summary>
        <table className="mt-2 w-full">
          <thead>
            <tr className="text-left">
              <th className="py-1 font-medium">Day (UTC)</th>
              <th className="py-1 text-right font-medium">Transfers</th>
              <th className="py-1 text-right font-medium">Volume</th>
            </tr>
          </thead>
          <tbody>
            {data.map((d) => (
              <tr key={d.date} className="border-t">
                <td className="py-1">{d.date}</td>
                <td className="py-1 text-right font-mono tabular">{d.count}</td>
                <td className="py-1 text-right font-mono tabular">{formatMinor(d.volume_minor)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </figure>
  );
}
