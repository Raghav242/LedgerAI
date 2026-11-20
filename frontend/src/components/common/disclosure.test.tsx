import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { Disclosure } from "./Disclosure";
import { StatusSummary } from "./StatusSummary";

describe("Disclosure", () => {
  it("hides secondary content until asked, and announces its state", async () => {
    const user = userEvent.setup();
    render(
      <Disclosure title="Technical details" meta="3 fields">
        <p>request id req-1</p>
      </Disclosure>,
    );
    const toggle = screen.getByRole("button", { name: /Technical details/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveTextContent("3 fields");
    expect(screen.queryByText("request id req-1")).toBeNull();
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("request id req-1")).toBeInTheDocument();
    expect(toggle).toHaveAttribute("aria-controls", screen.getByText("request id req-1").parentElement!.id);
  });

  it("works from the keyboard and can start open", async () => {
    const user = userEvent.setup();
    render(
      <Disclosure title="Why does it work?" defaultOpen>
        <p>row locks</p>
      </Disclosure>,
    );
    expect(screen.getByText("row locks")).toBeInTheDocument();
    await user.tab();
    await user.keyboard("{Enter}");
    expect(screen.queryByText("row locks")).toBeNull();
  });
});

describe("StatusSummary", () => {
  it("states the verdict in words, not colour alone", () => {
    render(<StatusSummary tone="destructive" title="Reconciliation failed" data-testid="v">2 discrepancies</StatusSummary>);
    expect(screen.getByTestId("v")).toHaveTextContent("Reconciliation failed2 discrepancies");
  });
});
