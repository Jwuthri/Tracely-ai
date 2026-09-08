import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Activation, type ActivationState } from "../Activation";
import type { Milestone } from "@/app/lib/api";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }) }));

const ms = (over: Partial<Record<string, { sample?: boolean }>> = {}): Milestone[] =>
  ["first_trace_received", "first_check_completed", "source_failure_confirmed", "case_reproduced", "candidate_verified", "ci_check_completed"].map((name) => ({
    name,
    first_at: name in over ? "2026-09-07T10:00:00+00:00" : null,
    sample: over[name]?.sample ?? null,
    integration: name in over ? "tracely" : "",
    elapsed_ms: null,
  }));

const state = (over: Partial<ActivationState> = {}): ActivationState => ({
  traces: 0, evaluators: 0, failures: 0, clusters: 0, cases: 0, gates: 0,
  ingestKey: "tracely_k_abc", endpoint: "https://api.example.com", milestones: ms(),
  ...over,
});

describe("Activation", () => {
  it("opens a brand-new workspace on step 1, with a runnable snippet for the real package", () => {
    render(<Activation {...state()} />);
    expect(screen.getByText("0 / 6")).toBeTruthy();
    const code = screen.getByText(/tracely.init/).textContent ?? "";
    expect(code).toContain('pip install "tracely-ai[openai]"');
    expect(code).toContain('api_key="tracely_k_abc"');
    expect(code).toContain('endpoint="https://api.example.com"');
    expect(screen.queryByText(/tracely replay/)).toBeNull();
    expect(screen.getByText("Try the sample")).toBeTruthy();
    expect(screen.getByText("Connect my agent")).toBeTruthy();
  });

  it("ticks from real milestones and shows when they happened", () => {
    render(<Activation {...state({ milestones: ms({ first_trace_received: {}, first_check_completed: {} }) })} />);
    expect(screen.getByText("2 / 6")).toBeTruthy();
    const list = within(screen.getByRole("list"));
    expect(list.getAllByText(/2026-09-07 10:00 · tracely/)).toHaveLength(2);
    expect(screen.getByText(/Promote a failing run/)).toBeTruthy(); // step 3 is current
  });

  it("sample milestones never count as activation", () => {
    render(
      <Activation
        {...state({
          traces: 900, cases: 3, gates: 2,
          milestones: ms({ first_trace_received: { sample: true }, source_failure_confirmed: { sample: true }, ci_check_completed: { sample: true } }),
        })}
      />,
    );
    expect(screen.getByText("0 / 6")).toBeTruthy();
    expect(screen.getByText(/Sample data is flowing/)).toBeTruthy();
  });

  it("falls back to counts for a workspace that predates milestones", () => {
    render(<Activation {...state({ traces: 1204, evaluators: 3, cases: 1 })} />);
    expect(screen.getByText("3 / 6")).toBeTruthy();
    const list = within(screen.getByRole("list"));
    expect(list.getByText("1,204 traces")).toBeTruthy();
  });

  it("disappears once every real milestone is in", () => {
    const all = ms(Object.fromEntries(["first_trace_received", "first_check_completed", "source_failure_confirmed", "case_reproduced", "candidate_verified", "ci_check_completed"].map((k) => [k, {}])));
    const { container } = render(<Activation {...state({ milestones: all })} />);
    expect(container.firstChild).toBeNull();
  });
});
