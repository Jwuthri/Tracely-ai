import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const createEvaluator = vi.fn(async (body: unknown) => ({ id: "e1", ...(body as object) }));

vi.mock("@/app/lib/evaluators", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  listEvaluators: vi.fn(async () => []),
  listTemplates: vi.fn(async () => []),
  createEvaluator: (body: unknown) => createEvaluator(body),
  listJudgeModels: vi.fn(async () => ({
    default: "openai/gpt-5.4-nano",
    models: [
      { id: "openai/gpt-5.4-nano", label: "GPT-5.4 Nano", kind: "llm", context_tokens: 400000 },
      { id: "google/gemini-3.6-flash", label: "Gemini 3.6 Flash", kind: "llm", context_tokens: 1000000 },
      { id: "typesafe/jev-1.13", label: "TypeSafe Jev 1.13", kind: "decision", context_tokens: 32000 },
    ],
  })),
}));

import { AddColumnModal } from "@/app/components/AddColumnModal";
import { applyDecision, decisionFromConfig, decisionProblem, EMPTY_DECISION } from "@/app/components/DecisionQuestionEditor";
import type { EvaluatorConfig } from "@/app/lib/evaluators";

beforeEach(() => {
  createEvaluator.mockClear();
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
});

describe("decision-model columns", () => {
  it("picking Jev rewords the form into a question + criteria and saves a decision config", async () => {
    render(<AddColumnModal open onClose={vi.fn()} onSaved={vi.fn()} />);
    fireEvent.click(screen.getByText("Manual"));
    fireEvent.change(screen.getByPlaceholderText("e.g., Helpfulness Score"), { target: { value: "Task done" } });

    const modelSelect = await waitFor(() => {
      const el = screen.getAllByRole("combobox")[0] as HTMLSelectElement;
      expect(el.querySelector('option[value="typesafe/jev-1.13"]')).not.toBeNull();
      return el;
    });
    fireEvent.change(modelSelect, { target: { value: "typesafe/jev-1.13" } });

    expect(screen.getByText("Question Type")).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText(/fully resolve/), { target: { value: "Did the agent finish the task?" } });
    fireEvent.change(screen.getByPlaceholderText("The answer does what was asked"), { target: { value: "Finished" } });
    fireEvent.change(screen.getByPlaceholderText("The answer misses or misreads it"), { target: { value: "Not finished" } });
    // the fallback list offers only LLMs with a bigger context — never the classifier itself
    const fallback = screen.getByDisplayValue(/Skip it/) as HTMLSelectElement;
    expect([...fallback.options].map((o) => o.value)).toEqual(["", "openai/gpt-5.4-nano", "google/gemini-3.6-flash"]);
    fireEvent.change(fallback, { target: { value: "google/gemini-3.6-flash" } });

    fireEvent.click(screen.getByText("Create Column"));
    await waitFor(() => expect(createEvaluator).toHaveBeenCalled());
    const cfg = (createEvaluator.mock.calls[0][0] as { config: EvaluatorConfig }).config;
    expect(cfg).toMatchObject({
      model: "typesafe/jev-1.13",
      output_type: "decision_binary",
      question: "Did the agent finish the task?",
      criteria: { true: "Finished", false: "Not finished" },
      pass_when: "yes",
      threshold: 0.5,
      fallback_model: "google/gemini-3.6-flash",
      execution_mode: "batch",
    });
    expect(cfg.prompt).toBe("");
  });

  it("round-trips label criteria and fail options", () => {
    const config: EvaluatorConfig = {
      output_type: "decision_multilabel",
      question: "What is the user asking about?",
      criteria: { refund: "money back", shipping: null },
      fail_options: ["refund"],
    };
    const fields = decisionFromConfig(config);
    expect(fields.labels).toEqual([
      { name: "refund", description: "money back", fail: true },
      { name: "shipping", description: "", fail: false },
    ]);
    const out: EvaluatorConfig = {};
    applyDecision(out, "decision_multilabel", fields);
    expect(out).toEqual({
      question: "What is the user asking about?",
      criteria: { refund: "money back", shipping: null },
      fail_options: ["refund"],
    });
  });

  it("catches invalid criteria before the request", () => {
    expect(decisionProblem("decision_binary", EMPTY_DECISION, "0.5")).toMatch(/question/);
    const q = { ...EMPTY_DECISION, question: "Q?" };
    const lbl = (name: string, fail = false) => ({ name, description: "", fail });
    expect(decisionProblem("decision_binary", { ...q, noulTrue: "y" }, "0.5")).toMatch(/Yes and a No/);
    expect(decisionProblem("decision_multiclass", { ...q, labels: [lbl("a")] }, "")).toMatch(/two labels/);
    // multi-class: every label failing means every answer fails; multi-label allows it
    const allFail = { ...q, labels: [lbl("a", true), lbl("b", true)] };
    expect(decisionProblem("decision_multiclass", allFail, "")).toMatch(/must not count/);
    expect(decisionProblem("decision_multilabel", allFail, "0.5")).toBe("");
    expect(decisionProblem("decision_multilabel", allFail, "1")).toMatch(/threshold/);
    expect(
      decisionProblem("decision_multilabel", { ...q, labels: Array.from({ length: 51 }, (_, i) => lbl(`l${i}`)) }, "0.5"),
    ).toMatch(/At most 50/);
  });

  it("offers Binary / Multi-class / Multi-label for a decision model", async () => {
    render(<AddColumnModal open onClose={vi.fn()} onSaved={vi.fn()} />);
    fireEvent.click(screen.getByText("Manual"));
    const modelSelect = await waitFor(() => {
      const el = screen.getAllByRole("combobox")[0] as HTMLSelectElement;
      expect(el.querySelector('option[value="typesafe/jev-1.13"]')).not.toBeNull();
      return el;
    });
    fireEvent.change(modelSelect, { target: { value: "typesafe/jev-1.13" } });
    const typeSelect = screen.getByDisplayValue("Binary (yes / no)") as HTMLSelectElement;
    expect([...typeSelect.options].map((o) => o.text)).toEqual([
      "Binary (yes / no)", "Multi-class (pick one)", "Multi-label (pick any)",
    ]);
  });
});
