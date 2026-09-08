/**
 * The plain-language reading of a case's assertions (W5 §2): what the fixed agent must do, as
 * sentences a reviewer can agree or disagree with — backed by the editable structured form.
 * Pure; unit-tested.
 */
export type ToolArgPredicate = { tool: string; key: string; equals: unknown };

export type Assertions = {
  required_tools?: string[];
  forbidden_tools?: string[];
  match_mode?: string;
  no_error?: boolean;
  allow_tool_errors?: boolean;
  max_tool_calls?: Record<string, number>;
  tool_args?: ToolArgPredicate[];
  quality?: { score_names?: string[] };
};

const MODE_TEXT: Record<string, string> = {
  superset: "in any order, other tools allowed",
  subset: "only these tools, any of them",
  strict: "exactly these tools, in this order",
  unordered: "exactly these tools, in any order",
};

export function describeExpectations(a: Assertions | null | undefined): string[] {
  const out: string[] = [];
  const x = a ?? {};
  const req = x.required_tools ?? [];
  if (req.length) {
    out.push(`Must call ${req.map((t) => `\`${t}\``).join(", ")} (${MODE_TEXT[x.match_mode ?? "superset"] ?? x.match_mode}).`);
  } else {
    out.push("No tool is required.");
  }
  if ((x.forbidden_tools ?? []).length) out.push(`Must never call ${x.forbidden_tools!.map((t) => `\`${t}\``).join(", ")}.`);
  for (const [tool, n] of Object.entries(x.max_tool_calls ?? {})) out.push(`\`${tool}\` at most ${n}×.`);
  for (const p of x.tool_args ?? []) out.push(`\`${p.tool}\` must be called with ${p.key} = ${JSON.stringify(p.equals)}.`);
  if (x.no_error === false) out.push("Errors are tolerated (the outcome is not asserted).");
  else if (x.allow_tool_errors) out.push("A tool may fail, but the run itself must complete without error.");
  else out.push("The run must complete without any error.");
  const judges = x.quality?.score_names ?? [];
  if (judges.length) out.push(`The answer must pass the ${judges.join(", ")} judge (semantic check, needs the workspace's LLM key).`);
  return out;
}
