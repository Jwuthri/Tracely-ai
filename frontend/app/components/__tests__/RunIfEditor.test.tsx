import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { RunIfEditor, labelsOf, runIfProblem } from "@/app/components/RunIfEditor";
import type { EvaluatorDef, RunIfCondition } from "@/app/lib/evaluators";

const intent: EvaluatorDef = {
  id: "1", name: "Conversation intent", description: "", kind: "llm_judge", score_name: "tracely.run.intent",
  level: "AGENT_RUN", enabled: true,
  config: { output_type: "decision_multiclass", criteria: { refund: null, greeting: null } },
};
const quality: EvaluatorDef = { ...intent, id: "2", name: "Answer quality", score_name: "tracely.run.quality",
  config: { output_type: "score", prompt: "Grade." } };

function Harness({ onValue }: { onValue: (v: RunIfCondition[]) => void }) {
  const [v, setV] = useState<RunIfCondition[]>([]);
  return <RunIfEditor candidates={[intent, quality]} value={v} onChange={(n) => { setV(n); onValue(n); }} />;
}

describe("RunIfEditor", () => {
  it("offers a decision column's labels and builds a label condition", () => {
    let last: RunIfCondition[] = [];
    render(<Harness onValue={(v) => (last = v)} />);
    fireEvent.click(screen.getByText("+ Add condition"));
    expect(last).toEqual([{ column: "tracely.run.intent", field: "label", op: "in", values: ["refund"] }]);
    fireEvent.click(screen.getByText("greeting"));
    expect(last[0].values).toEqual(["refund", "greeting"]);
    // switching to a column without labels starts on its verdict
    fireEvent.change(screen.getByLabelText("Condition 1 column"), { target: { value: "tracely.run.quality" } });
    expect(last[0]).toMatchObject({ column: "tracely.run.quality", field: "verdict", values: ["FAIL"] });
  });

  it("knows where labels come from and validates", () => {
    expect(labelsOf(intent)).toEqual(["refund", "greeting"]);
    expect(runIfProblem([{ column: "c", field: "label", op: "in", values: [] }])).toMatch(/at least one label/);
    expect(runIfProblem([{ column: "c", field: "value", op: "lt" }])).toMatch(/number/);
  });
});
