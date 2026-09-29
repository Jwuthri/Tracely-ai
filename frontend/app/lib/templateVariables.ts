// ── Advanced-mode @VARIABLE catalog (client mirror of backend template_resolver.TEMPLATE_VARIABLES) ──
// The editor autocompletes / highlights these; the backend resolves them against real trace data.
// Keep in sync with backend/tracely/domain/evaluation/template_resolver.py (or fetch
// /api/evaluators/template-variables/{level} — the shapes match).

import type { EvaluatorLevel } from "./evaluators";

export type CatalogLevel = "conversation" | "message" | "step";

export type TemplateVarProp = { name: string; description: string };

export type TemplateVar = {
  name: string;
  description: string;
  type: "string" | "object";
  levels: CatalogLevel[];
  props?: TemplateVarProp[];
  sequentialOnly?: boolean;
};

// Group 1 = UPPERCASE name, group 2 = optional lowercase `.property`. Same regex the backend uses.
export const VARIABLE_RE = /@([A-Z_]+)(?:\.([a-z_]+))?/g;

// `@CURRENT_STEPS.<type>` — the turn's steps of one type. The backend matches any lowercase prop
// against the span type, so this list is autocomplete, not a whitelist.
const STEP_TYPE_PROPS: TemplateVarProp[] = [
  { name: "tool", description: "Only the TOOL steps — each call with its arguments and result" },
  { name: "retriever", description: "Only the RETRIEVER steps — the retrieved context" },
  { name: "generation", description: "Only the GENERATION steps — the model calls" },
  { name: "thinking", description: "Only the THINKING steps — the agent's reasoning" },
  { name: "chain", description: "Only the CHAIN steps" },
  { name: "skill", description: "Only the SKILL steps — the named capabilities the agent ran" },
  { name: "delegate", description: "Only the DELEGATE steps — handovers to another agent" },
];

const STEP_PROPS: TemplateVarProp[] = [
  { name: "tool_call", description: "The tool invocation (name + arguments) at this step" },
  { name: "tool_result", description: "The tool's returned result" },
  { name: "thinking", description: "The step's reasoning/thinking text (THINKING steps)" },
  { name: "output_content", description: "The readable text output of the step" },
  { name: "output_structured", description: "The raw structured (JSON) output of the step" },
  { name: "input", description: "The step's readable input (tool arguments, a model call's messages, a query, a task)" },
  { name: "error", description: "The step's error message, when it failed" },
];

const ALL: CatalogLevel[] = ["conversation", "message", "step"];

export const TEMPLATE_VARIABLES: TemplateVar[] = [
  // common (all levels)
  { name: "HISTORY", description: "Full formatted conversation history", type: "string", levels: ALL },
  { name: "ROLLING_SUMMARY", description: "Accumulated rolling summary of the conversation so far (compact, prefix-stable); empty when none has been generated", type: "string", levels: ALL },
  { name: "GOAL", description: "User's overall goal/intent (first request in the thread)", type: "string", levels: ALL },
  {
    name: "AGENTS",
    description: "The agents of this conversation, each with its description and tools — declared, offered to the model, or only seen being called",
    type: "object",
    levels: ALL,
    props: [
      { name: "tools", description: "Every tool the agents had, with where it came from and how often it was called" },
      { name: "called", description: "Only the tools that were called, with how many times" },
    ],
  },
  { name: "DEPENDENCIES", description: "This item's results from the columns in Depends On — label, value, verdict and reason each", type: "string", levels: ALL },
  // conversation only
  { name: "MESSAGES", description: "All turns formatted ([role]: text)", type: "string", levels: ["conversation"] },
  { name: "USER_MESSAGES", description: "All user requests only", type: "string", levels: ["conversation"] },
  { name: "ASSISTANT_MESSAGES", description: "All assistant answers only", type: "string", levels: ["conversation"] },
  { name: "FIRST_USER_MSG", description: "The first user request", type: "string", levels: ["conversation"] },
  { name: "LAST_USER_MSG", description: "The last user request", type: "string", levels: ["conversation"] },
  { name: "LAST_ASSISTANT_MSG", description: "The last assistant answer", type: "string", levels: ["conversation"] },
  // message level (step inherits)
  { name: "PREVIOUS_USER_MSG", description: "The previous turn's user request", type: "string", levels: ["message", "step"] },
  { name: "PREVIOUS_ASSISTANT_MSG", description: "The previous turn's assistant answer", type: "string", levels: ["message", "step"] },
  {
    name: "CURRENT_MESSAGE", description: "The turn under evaluation", type: "object", levels: ["message", "step"],
    props: [
      { name: "input", description: "The user request" },
      { name: "output", description: "The assistant answer" },
      { name: "role", description: "Always 'assistant'" },
    ],
  },
  {
    name: "CURRENT_STEPS",
    description: "All steps of the current turn, formatted — narrow it to one step type",
    type: "object",
    levels: ["message", "step"],
    props: STEP_TYPE_PROPS,
  },
  { name: "CURRENT_STEPS_COUNT", description: "Number of steps in the current turn", type: "string", levels: ["message", "step"] },
  // step only
  { name: "PREVIOUS_STEP", description: "The previous step in this turn", type: "object", levels: ["step"], props: STEP_PROPS },
  { name: "CURRENT_STEP", description: "The step under evaluation", type: "object", levels: ["step"], props: STEP_PROPS },
  { name: "STEP_NUMBER", description: "1-indexed position of the current step", type: "string", levels: ["step"] },
  // sequential mode only (message + step)
  {
    name: "METRIC_PREVIOUS_RESULT", description: "The previous item's result of this metric (sequential mode)",
    type: "string", levels: ["message", "step"], sequentialOnly: true,
  },
];

const BY_NAME = new Map(TEMPLATE_VARIABLES.map((v) => [v.name, v]));

export function catalogLevel(level: EvaluatorLevel | string): CatalogLevel {
  if (level === "CONVERSATION") return "conversation";
  if (level === "AGENT_RUN") return "message";
  return "step"; // SPAN / TOOL / GENERATION / CHAIN
}

// The variables offered at an evaluator level. `includeSequential=false` hides @METRIC_PREVIOUS_RESULT
// (only meaningful when the column runs in sequential mode).
export function getVariablesForLevel(level: EvaluatorLevel | string, includeSequential = true): TemplateVar[] {
  const cl = catalogLevel(level);
  return TEMPLATE_VARIABLES.filter(
    (v) => v.levels.includes(cl) && (includeSequential || !v.sequentialOnly),
  );
}

export function getVariable(name: string): TemplateVar | undefined {
  return BY_NAME.get(name);
}

// A prompt is advanced as soon as it carries any @VARIABLE (mirrors the backend's server-side recompute).
export function hasTemplateVariables(prompt: string): boolean {
  return /@[A-Z_]+/.test(prompt || "");
}

// De-duplicated refs used in `prompt` (e.g. ["HISTORY", "CURRENT_STEP.tool_call"]).
export function extractVariablesFromPrompt(prompt: string): string[] {
  const out: string[] = [];
  const re = new RegExp(VARIABLE_RE.source, "g");
  let m: RegExpExecArray | null;
  while ((m = re.exec(prompt || "")) !== null) {
    const ref = m[1] + (m[2] ? `.${m[2]}` : "");
    if (!out.includes(ref)) out.push(ref);
  }
  return out;
}
