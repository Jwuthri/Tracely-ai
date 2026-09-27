"use client";

import clsx from "clsx";
import { useEffect, useMemo, useRef, useState, type SVGProps } from "react";
import { createPortal } from "react-dom";
import {
  createEvaluator,
  generateEvaluator,
  listEvaluators,
  listJudgeModels,
  listTemplates,
  updateEvaluator,
  type EvaluatorConfig,
  type EvaluatorDef,
  type EvaluatorDraft,
  type EvaluatorLevel,
  type EvaluatorTemplate,
  type JudgeModelOption,
  type JudgeModels,
  type RunIfCondition,
  isDecisionOutput,
} from "../lib/evaluators";
import { extractVariablesFromPrompt, hasTemplateVariables } from "../lib/templateVariables";
import { AdvancedPromptEditor } from "./AdvancedPromptEditor";
import {
  DECISION_TYPE_OPTIONS,
  DecisionQuestionEditor,
  EMPTY_DECISION,
  applyDecision,
  clearDecision,
  decisionFromConfig,
  decisionProblem,
  type DecisionFields,
} from "./DecisionQuestionEditor";
import { OutputSchemaBuilder } from "./OutputSchemaBuilder";
import { RunIfEditor, runIfProblem } from "./RunIfEditor";
import { PromptPreview } from "./PromptPreview";

// ── Add Evaluation Column ───────────────────────────────────────────────────────
// The TurnWise-style wizard: Browse Library (one click) · Manual (form) · Use AI
// (describe → draft → review in the form). JSON Object outputs get a dedicated schema
// step (the OutputSchemaBuilder). Editing an existing column jumps straight to the form.
// Creation never runs anything — columns evaluate on ingest + via Run.

const svg = (p: SVGProps<SVGSVGElement>) => ({
  xmlns: "http://www.w3.org/2000/svg",
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  ...p,
});
const BookIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...svg(p)}><path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20" /></svg>
);
const CodeIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...svg(p)}><path d="m18 16 4-4-4-4" /><path d="m6 8-4 4 4 4" /><path d="m14.5 4-5 16" /></svg>
);
const SparklesIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...svg(p)}>
    <path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z" />
    <path d="M20 3v4" /><path d="M22 5h-4" />
  </svg>
);
const XIcon = (p: SVGProps<SVGSVGElement>) => (
  <svg {...svg(p)}><path d="M18 6 6 18" /><path d="m6 6 12 12" /></svg>
);
const ChevronR = (p: SVGProps<SVGSVGElement>) => (
  <svg {...svg(p)}><path d="m9 18 6-6-6-6" /></svg>
);

type Step = "type" | "library" | "config" | "schema";

const LEVEL_OPTIONS: { value: EvaluatorLevel; label: string; hint: string }[] = [
  { value: "CONVERSATION", label: "Conversation", hint: "one grade per thread" },
  { value: "AGENT_RUN", label: "Message", hint: "one grade per turn" },
  { value: "SPAN", label: "Step", hint: "one grade per tool call / generation" },
];

// Color palette matching the table's level badge / row depth system.
const LEVEL_COLORS: Record<string, { active: string; dot: string; lborder: string }> = {
  CONVERSATION: {
    active:  "border-info/50 bg-info/12 text-info",
    dot:     "bg-info",
    lborder: "border-l-info",
  },
  AGENT_RUN: {
    active:  "border-ok/50 bg-ok/12 text-ok",
    dot:     "bg-ok",
    lborder: "border-l-ok",
  },
  SPAN: {
    active:  "border-t_think/50 bg-t_think/12 text-t_think",
    dot:     "bg-t_think",
    lborder: "border-l-t_think",
  },
};
const STEP_LABELS: Record<Step, string> = {
  type:    "Choose type",
  library: "Template library",
  config:  "Configure",
  schema:  "Output schema",
};
// Step-flavored levels all live in the S group; the segmented control canonicalizes to SPAN
// unless the evaluator already uses a narrower step level.
const S_LEVELS = new Set<EvaluatorLevel>(["SPAN", "TOOL", "GENERATION", "CHAIN"]);

const OUTPUT_OPTIONS = [
  { value: "score", label: "Score (0–1 + threshold)" },
  { value: "number", label: "Number" },
  { value: "boolean", label: "Pass / Fail" },
  { value: "text", label: "Text" },
  { value: "json", label: "JSON Object (+ custom fields)" },
  // decision-model question types (offered only when a decision model is picked)
  ...DECISION_TYPE_OPTIONS.map((o) => ({ value: o.value, label: o.label })),
] as const;
const LLM_OUTPUT_VALUES = new Set(["score", "number", "boolean", "text", "json"]);

// Decision models (TypeSafe Jev) are classifiers: they answer one typed question instead of
// grading a rubric, so picking one swaps the rubric fields for a question + criteria editor.
function isDecisionModelId(models: JudgeModelOption[] | undefined, id: string): boolean {
  return Boolean(id) && (models ?? []).some((m) => m.id === id && m.kind === "decision");
}

function fmtContext(tokens: number | null | undefined): string {
  if (!tokens) return "";
  return tokens >= 1_000_000 ? `${Math.round(tokens / 100_000) / 10}M` : `${Math.round(tokens / 1000)}k`;
}

// Which step span types a Step (SPAN) judge grades — stored as `config.span_types` and honored
// by the backend `_run_steps` filter + the table's per-row blanking. Matches the ingest
// vocabulary (otel/types.py); REASONING is collapsed to THINKING at ingestion.
const SPAN_TYPE_OPTIONS = [
  { value: "TOOL", label: "Tool" },
  { value: "GENERATION", label: "Generation" },
  { value: "CHAIN", label: "Chain" },
  { value: "THINKING", label: "Thinking" },
] as const;
const DEFAULT_SPAN_TYPES = ["TOOL", "GENERATION"];

type FormState = {
  name: string;
  description: string;
  kind: "llm_judge" | "structural";
  level: EvaluatorLevel;
  prompt: string;
  model: string; // "" = project default
  outputType: (typeof OUTPUT_OPTIONS)[number]["value"];
  executionMode: "batch" | "sequential";
  dependsOn: string[]; // score_names of evaluators whose results are injected as context
  runIf: RunIfCondition[]; // grade only when these hold on the Depends On results
  threshold: string;
  spanTypes: string[]; // SPAN-level judges: which step types to grade (config.span_types)
  outputSchema?: Record<string, unknown>;
  fallbackModel: string; // "" = skip items too long for `model`'s context
  decision: DecisionFields; // decision-model columns (question + criteria)
  paramsJson: string; // structural evaluators only
  enabled: boolean;
  scoreName?: string; // set when installing a template (stable identity)
  // The config this form was hydrated from (template / AI draft / edited row). Save spreads it
  // first, so keys the form doesn't surface — structural `check`, judge `max_spans` — survive
  // installs and edits instead of being silently dropped.
  sourceConfig?: EvaluatorConfig;
};

const EMPTY_FORM: FormState = {
  name: "", description: "", kind: "llm_judge", level: "AGENT_RUN", prompt: "", model: "",
  outputType: "score", executionMode: "batch", dependsOn: [], runIf: [], threshold: "0.6",
  spanTypes: DEFAULT_SPAN_TYPES, outputSchema: undefined, fallbackModel: "", decision: EMPTY_DECISION,
  paramsJson: "", enabled: true,
};

function formFromConfig(
  base: Pick<FormState, "name" | "description" | "kind" | "level" | "enabled">,
  config: EvaluatorConfig,
  scoreName?: string,
): FormState {
  const outputType = config.output_type;
  return {
    ...EMPTY_FORM,
    ...base,
    prompt: config.prompt ?? "",
    model: config.model ?? "",
    outputType: (outputType as FormState["outputType"]) ?? "score",
    executionMode: config.execution_mode === "sequential" ? "sequential" : "batch",
    dependsOn: config.depends_on ?? [],
    runIf: config.run_if ?? [],
    spanTypes: (config.span_types ?? []).length ? config.span_types! : DEFAULT_SPAN_TYPES,
    // no-threshold configs stay threshold-less (informational metrics keep their no-verdict
    // semantics through install/edit round-trips) — only brand-new forms default to 0.6
    threshold: config.threshold != null ? String(config.threshold) : "",
    outputSchema: config.output_schema as Record<string, unknown> | undefined,
    fallbackModel: config.fallback_model ?? "",
    decision: isDecisionOutput(config.output_type) ? decisionFromConfig(config) : EMPTY_DECISION,
    paramsJson: config.check ? JSON.stringify(config.params ?? {}, null, 2) : "",
    scoreName,
    sourceConfig: config,
  };
}

function configFromForm(f: FormState, previous?: EvaluatorConfig): EvaluatorConfig {
  if (f.kind === "structural") {
    const config: EvaluatorConfig = { ...(previous ?? {}) };
    if (f.paramsJson.trim()) config.params = JSON.parse(f.paramsJson) as Record<string, unknown>;
    return config;
  }
  // preserve advanced keys we don't surface (span_types, max_spans, …) when editing
  const config: EvaluatorConfig = { ...(previous ?? {}) };
  delete config.check;
  delete config.params;
  config.prompt = f.prompt.trim();
  config.output_type = f.outputType;
  config.execution_mode = f.executionMode;
  // Advanced-ness follows the prompt's @VARIABLEs (the backend recomputes this authoritatively on
  // save — mirror it here so the optimistic column object matches what's persisted).
  const templateVars = extractVariablesFromPrompt(config.prompt);
  if (templateVars.length > 0) {
    config.is_advanced = true;
    config.template_variables = templateVars;
  } else {
    delete config.is_advanced;
    delete config.template_variables;
  }
  if (f.model.trim()) config.model = f.model.trim();
  else delete config.model;
  if (f.fallbackModel.trim()) config.fallback_model = f.fallbackModel.trim();
  else delete config.fallback_model;
  if (isDecisionOutput(f.outputType)) {
    applyDecision(config, f.outputType, f.decision);
    const t = parseFloat(f.threshold);
    if (f.outputType === "decision_multiclass" || Number.isNaN(t)) delete config.threshold;
    else config.threshold = Math.min(Math.max(t, 0), 1);
  } else {
    clearDecision(config);
  }
  if (isDecisionOutput(f.outputType)) {
    // threshold handled above
  } else if (f.outputType === "score" || f.outputType === "number" || f.outputType === "json") {
    const t = parseFloat(f.threshold);
    // blank = deliberately no threshold (informational metric, no PASS/FAIL)
    if (f.threshold.trim() !== "" && !Number.isNaN(t)) {
      config.threshold = f.outputType === "number" ? t : Math.min(Math.max(t, 0), 1);
    } else {
      delete config.threshold;
    }
  } else {
    delete config.threshold;
  }
  if (f.outputType === "json" && f.outputSchema) config.output_schema = f.outputSchema;
  else delete config.output_schema;
  // Step (SPAN) judges grade a chosen set of step types; the backend honors span_types only at
  // SPAN level, so only persist it there (and drop it if the metric moved to a coarser level).
  if (S_LEVELS.has(f.level)) config.span_types = f.spanTypes;
  else delete config.span_types;
  // a condition's column must run first — it joins Depends On (the backend does the same)
  const deps = [...f.dependsOn, ...f.runIf.map((c) => c.column).filter((c) => !f.dependsOn.includes(c))];
  if (deps.length > 0) config.depends_on = [...new Set(deps)];
  else delete config.depends_on;
  if (f.runIf.length > 0) config.run_if = f.runIf;
  else delete config.run_if;
  return config;
}

// Which prompt tab a seeded config opens on: advanced when it's flagged or its prompt carries @VARIABLEs.
function modeForConfig(config: EvaluatorConfig): "basic" | "advanced" {
  return config.is_advanced || hasTemplateVariables(config.prompt ?? "") ? "advanced" : "basic";
}

export function AddColumnModal({
  open,
  onClose,
  onSaved,
  editing,
  prefill,
  previewThread,
}: {
  open: boolean;
  onClose: () => void;
  onSaved: (ev: EvaluatorDef) => void;
  editing?: EvaluatorDef | null;
  prefill?: EvaluatorDraft | null; // a draft to seed a NEW column (e.g. a cluster's suggested evaluator)
  previewThread?: string; // a conversation id the advanced preview defaults to (what the user is viewing)
}) {
  const [step, setStep] = useState<Step>("type");
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  // Basic = plain rubric (trace context auto-appended); Advanced = @VARIABLE template the user controls.
  const [promptMode, setPromptMode] = useState<"basic" | "advanced">("basic");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // Use AI
  const [aiOpen, setAiOpen] = useState(false);
  const [aiText, setAiText] = useState("");
  const [aiBusy, setAiBusy] = useState(false);
  // Whether the workspace has an OpenRouter key. The AI-generate path (and every LLM judge the
  // modal creates) runs on it exclusively; null = probe still in flight, treat as configured.
  const [hasLlmKey, setHasLlmKey] = useState<boolean | null>(null);
  // Library + model choices + existing evaluators (for depends_on)
  const [templates, setTemplates] = useState<EvaluatorTemplate[] | null>(null);
  const [judgeModels, setJudgeModels] = useState<JudgeModels | null>(null);
  const [allEvaluators, setAllEvaluators] = useState<EvaluatorDef[] | null>(null);
  // The form exactly as it was seeded on open. Anything else means unsaved work, and closing
  // has to ask first — a half-written rubric is 5 minutes of typing, not a stray click.
  const seededForm = useRef("");

  // (Re)seed on open: editing jumps straight to the form. Templates refetch every open so
  // `installed` flags stay truthful after a save; models refetch until a non-empty list lands.
  useEffect(() => {
    if (!open) return;
    setError("");
    setAiOpen(false);
    setAiText("");
    setBusy(false);
    setTemplates(null);
    setAllEvaluators(null);
    void listEvaluators().then(setAllEvaluators).catch(() => setAllEvaluators([]));
    void fetch("/api/project/llm-key", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setHasLlmKey(d ? Boolean(d.configured) : null))
      .catch(() => setHasLlmKey(null));
    if (judgeModels === null || judgeModels.models.length === 0) {
      void listJudgeModels().then(setJudgeModels).catch(() => {});
    }
    if (editing) {
      setForm(seed(formFromConfig(
        {
          name: editing.name, description: editing.description, kind: editing.kind,
          level: editing.level, enabled: editing.enabled,
        },
        editing.config ?? {},
      )));
      setPromptMode(modeForConfig(editing.config ?? {}));
      setStep("config");
    } else if (prefill) {
      // a suggested/draft evaluator (e.g. from a failure cluster): seed the form for review, skip
      // the type picker, land on Configure so the user confirms/edits before creating.
      setForm(seed(formFromConfig(
        {
          name: prefill.name, description: prefill.description, kind: prefill.kind,
          level: prefill.level, enabled: true,
        },
        prefill.config ?? {},
        prefill.score_name,
      )));
      setPromptMode(modeForConfig(prefill.config ?? {}));
      setStep("config");
    } else {
      setForm(seed(EMPTY_FORM));
      setPromptMode("basic");
      setStep("type");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editing, prefill]);

  // A brand-new column starts on the workspace default model. When that is a decision model the
  // form has to open on its question types — the rubric output types can't run on it. Only an
  // untouched new form moves (never an edit, a template, or an AI draft), and the pristine
  // baseline moves with it so this doesn't count as unsaved work.
  useEffect(() => {
    if (!open || editing || prefill || !judgeModels) return;
    if (!isDecisionModelId(judgeModels.models, judgeModels.default)) return;
    setForm((f) => {
      if (f.model || isDecisionOutput(f.outputType) || JSON.stringify(f) !== seededForm.current) return f;
      return seed({ ...f, outputType: "decision_binary", threshold: "0.5" });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, judgeModels, editing, prefill]);

  useEffect(() => {
    if (open && step === "library" && templates === null) {
      void listTemplates().then(setTemplates).catch(() => setTemplates([]));
    }
  }, [open, step, templates]);

  const grouped = useMemo(() => {
    const tpl = templates ?? [];
    const groups: { key: string; label: string; items: EvaluatorTemplate[] }[] = [
      { key: "C", label: "Conversation level", items: [] },
      { key: "M", label: "Message level", items: [] },
      { key: "S", label: "Step level", items: [] },
    ];
    for (const t of tpl) {
      const g = t.level === "CONVERSATION" ? 0 : t.level === "AGENT_RUN" ? 1 : 2;
      groups[g].items.push(t);
    }
    return groups.filter((g) => g.items.length > 0);
  }, [templates]);

  // Remember a freshly seeded form as the pristine baseline, and pass it through to setForm.
  function seed(f: FormState): FormState {
    seededForm.current = JSON.stringify(f);
    return f;
  }

  // The only way out. Backdrop clicks don't close this modal at all (too easy to lose a rubric
  // to a stray click or a text selection dragged past the edge); X and Esc confirm if dirty.
  function requestClose() {
    const dirty = JSON.stringify(form) !== seededForm.current || aiText.trim() !== "";
    if (!dirty || window.confirm("Discard this evaluation column? Your changes will be lost.")) onClose();
  }

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") requestClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  if (!open) return null;
  if (typeof window === "undefined") return null;

  function pickTemplate(t: EvaluatorTemplate) {
    if (t.installed) return;
    setForm(formFromConfig(
      { name: t.name, description: t.description, kind: t.kind, level: t.level, enabled: true },
      t.config ?? {},
      t.score_name,
    ));
    setPromptMode(modeForConfig(t.config ?? {}));
    setStep("config");
  }

  async function runGenerate() {
    if (!aiText.trim()) return;
    setAiBusy(true);
    setError("");
    try {
      const draft: EvaluatorDraft = await generateEvaluator(aiText.trim());
      setForm(formFromConfig(
        {
          name: draft.name, description: draft.description, kind: draft.kind,
          level: draft.level, enabled: true,
        },
        draft.config ?? {},
      ));
      setPromptMode(modeForConfig(draft.config ?? {}));
      setStep("config");
    } catch (e) {
      setError(e instanceof Error ? e.message : "generation failed");
    } finally {
      setAiBusy(false);
    }
  }

  function submitConfig() {
    if (!form.name.trim()) {
      setError("Name is required.");
      return;
    }
    const decisionCol = form.kind === "llm_judge" && isDecisionOutput(form.outputType);
    if (decisionCol) {
      if (promptMode === "advanced" && !form.prompt.trim()) {
        setError("Write the state template, or switch to Basic to send the item as-is.");
        return;
      }
      const problem = decisionProblem(form.outputType as Parameters<typeof decisionProblem>[0], form.decision, form.threshold);
      if (problem) {
        setError(problem);
        return;
      }
    } else if (form.kind === "llm_judge" && !form.prompt.trim()) {
      setError("Evaluation prompt is required.");
      return;
    }
    const condProblem = runIfProblem(form.runIf);
    if (form.kind === "llm_judge" && condProblem) {
      setError(condProblem);
      return;
    }
    if (form.kind === "llm_judge" && S_LEVELS.has(form.level) && form.spanTypes.length === 0) {
      setError("Select at least one step type to grade.");
      return;
    }
    setError("");
    if (form.kind === "llm_judge" && form.outputType === "json") {
      setStep("schema");
      return;
    }
    void save();
  }

  function schemaPropertyCount(schema: Record<string, unknown> | undefined): number {
    const props = schema && typeof schema === "object" ? (schema as { properties?: object }).properties : undefined;
    return props ? Object.keys(props).length : 0;
  }

  async function save() {
    if (form.kind === "llm_judge" && form.outputType === "json" && schemaPropertyCount(form.outputSchema) === 0) {
      setError("Define at least one output field, or switch to the Score / Pass-Fail output type.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const config = configFromForm(
        // basic decision columns read the item itself; a rubric typed before switching models
        // would only be dead weight (or, with @VARIABLEs, silently turn the column advanced)
        isDecisionOutput(form.outputType) && promptMode === "basic" ? { ...form, prompt: "" } : form,
        editing?.config ?? form.sourceConfig,
      );
      const saved = editing
        ? await updateEvaluator(editing.id, {
            name: form.name.trim(),
            description: form.description.trim(),
            level: form.level,
            enabled: form.enabled,
            config,
          })
        : await createEvaluator({
            name: form.name.trim(),
            description: form.description.trim(),
            kind: form.kind,
            level: form.level,
            config,
            enabled: form.enabled,
            ...(form.scoreName ? { score_name: form.scoreName } : {}),
          });
      onSaved(saved);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "save failed");
    } finally {
      setBusy(false);
    }
  }

  const isStructural = form.kind === "structural";
  const levelSegment: EvaluatorLevel = S_LEVELS.has(form.level) ? "SPAN" : form.level;
  const levelColors = LEVEL_COLORS[levelSegment] ?? LEVEL_COLORS.AGENT_RUN;
  const saveLabel = editing ? "Save Changes" : "Create Column";
  const isDecisionCol = isDecisionOutput(form.outputType);
  // "" = the default for this KIND of column: a decision column runs on the decision default
  // (Jev), an LLM column on the text default — the backend resolves it the same way.
  const defaultId = (isDecisionCol ? judgeModels?.default : judgeModels?.text_default ?? judgeModels?.default) ?? "";
  const defaultModelLabel = defaultId ? `Default — ${defaultId}` : "Default judge model";
  const modelOptions = judgeModels?.models ?? [];
  // "" = the workspace default, which is the decision model when one is reachable
  const effectiveModel = form.model || defaultId;
  const decisionModel = isDecisionModelId(modelOptions, effectiveModel);
  const primaryInfo = modelOptions.find((m) => m.id === effectiveModel);
  // Fallback candidates: text models that can read MORE than the primary (all of them when the
  // primary's context is unknown), never the primary itself.
  const fallbackOptions = modelOptions.filter(
    (m) =>
      m.kind !== "decision" &&
      m.id !== effectiveModel &&
      (!primaryInfo?.context_tokens || !m.context_tokens || m.context_tokens > primaryInfo.context_tokens),
  );

  // Switching between an LLM judge and a decision model swaps the output vocabulary: carry what
  // transfers (name, level, prompt) and reset what doesn't, so the form never holds a pair the
  // backend rejects (a rubric output on Jev, a decision question on an LLM).
  function pickModel(id: string) {
    setForm((f) => {
      // "Default" keeps the column's kind (its default is of that kind); a named model decides it
      const toDecision = id ? isDecisionModelId(modelOptions, id) : isDecisionOutput(f.outputType);
      if (toDecision && !isDecisionOutput(f.outputType)) {
        return { ...f, model: id, outputType: "decision_binary", threshold: "0.5" };
      }
      if (!toDecision && isDecisionOutput(f.outputType)) {
        const hasCriteria = f.decision.question.trim() !== "";
        if (hasCriteria && !window.confirm("Switch to an LLM judge? The question and criteria will be discarded.")) return f;
        return { ...f, model: id, outputType: "score", threshold: "0.6", decision: EMPTY_DECISION };
      }
      return { ...f, model: id };
    });
  }

  // Breadcrumb: for the "library" path show type→library→config; for direct skip just show the relevant steps.
  const visibleSteps: Step[] = editing
    ? ["config", ...(form.outputType === "json" ? ["schema" as Step] : [])]
    : step === "library" || (step === "config" && !editing)
    ? ["type", "library", "config", ...(form.outputType === "json" ? ["schema" as Step] : [])]
    : ["type", "config", ...(form.outputType === "json" ? ["schema" as Step] : [])];

  return createPortal(
    <div
      className="fixed inset-0 z-[90] flex items-start justify-center overflow-y-auto bg-black/75 p-4 backdrop-blur-sm sm:p-8"
      role="dialog"
      aria-modal="true"
      aria-label={editing ? "Edit evaluation column" : "Add evaluation column"}
    >
      <div
        className={clsx(
          "mt-4 w-full max-w-xl overflow-hidden rounded-xl border border-line bg-ink-900 shadow-pop",
          "border-l-4 transition-colors duration-300",
          levelColors.lborder,
        )}
      >
        <div className="border-b border-line px-5 py-4">
          <div className="flex items-center justify-between">
            <h2 className="text-[13.5px] font-semibold tracking-tight text-fg">
              {editing ? "Edit Evaluation Column" : "Add Evaluation Column"}
            </h2>
            <button onClick={requestClose} className="rounded-md p-1 text-fg-faint transition-colors hover:bg-ink-700 hover:text-fg-muted" aria-label="Close">
              <XIcon className="h-3.5 w-3.5" />
            </button>
          </div>
          {/* Step breadcrumb */}
          <div className="mt-1 flex items-center gap-1">
            {visibleSteps.map((s, i) => (
              <span key={s} className="flex items-center gap-1">
                {i > 0 && <span className="text-[10px] text-line-bright">›</span>}
                <span className={clsx(
                  "text-[10.5px]",
                  s === step ? "text-fg-muted" : "text-fg-faint/70",
                )}>
                  {STEP_LABELS[s]}
                </span>
              </span>
            ))}
          </div>
        </div>

        <div className="max-h-[75vh] overflow-y-auto p-5">
          {step === "type" && (
            <div className="space-y-4">
              <p className="text-[12.5px] text-fg-muted">Choose how you want to create your evaluation metric.</p>

              {/* Browse Library — primary full-width card */}
              <button
                onClick={() => setStep("library")}
                className="group flex w-full items-center gap-3 rounded-lg border border-line-bright/60 bg-ink-700/50 p-4 text-left transition-colors hover:border-ok/40 hover:bg-ink-600/50"
              >
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-ok/15 text-ok">
                  <BookIcon className="h-4.5 w-4.5" />
                </span>
                <span className="min-w-0">
                  <span className="block text-[13px] font-medium text-fg">Browse Library</span>
                  <span className="block text-[11.5px] text-fg-muted">Choose from pre-built evaluation metrics</span>
                </span>
                <ChevronR className="ml-auto h-4 w-4 shrink-0 text-fg-faint transition-colors group-hover:text-fg-muted" />
              </button>

              <div className="flex items-center gap-3 text-[10px] uppercase tracking-widest text-fg-faint">
                <span className="h-px flex-1 bg-line" />
                or create your own
                <span className="h-px flex-1 bg-line" />
              </div>

              {/* Manual + AI cards */}
              <div className="grid grid-cols-2 gap-3">
                <button
                  onClick={() => { setForm(EMPTY_FORM); setPromptMode("basic"); setStep("config"); }}
                  className="group flex flex-col items-center gap-2.5 rounded-lg border border-line bg-ink-700/40 p-5 text-center transition-colors hover:border-line-bright hover:bg-ink-600/50"
                >
                  <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-info/15 text-info">
                    <CodeIcon className="h-5 w-5" />
                  </span>
                  <span>
                    <span className="block text-[12.5px] font-medium text-fg">Manual</span>
                    <span className="mt-0.5 block text-[11px] leading-snug text-fg-muted">Define prompt and output format</span>
                  </span>
                </button>
                <button
                  onClick={() => setAiOpen((o) => !o)}
                  className={clsx(
                    "group flex flex-col items-center gap-2.5 rounded-lg border p-5 text-center transition-colors",
                    aiOpen
                      ? "border-t_think/50 bg-t_think/[0.07]"
                      : "border-line bg-ink-700/40 hover:border-line-bright hover:bg-ink-600/50",
                  )}
                >
                  <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-t_think/15 text-t_think">
                    <SparklesIcon className="h-5 w-5" />
                  </span>
                  <span>
                    <span className="block text-[12.5px] font-medium text-fg">Use AI</span>
                    <span className="mt-0.5 block text-[11px] leading-snug text-fg-muted">Describe and let AI create it</span>
                  </span>
                </button>
              </div>

              {aiOpen && hasLlmKey === false && (
                <p className="rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-[12.5px] text-warn">
                  AI generation (and every LLM judge) runs on your workspace&apos;s own OpenRouter
                  key —{" "}
                  <a href="/settings/llm" className="underline underline-offset-2">
                    add one in Settings
                  </a>{" "}
                  first. Structural evaluators work without it.
                </p>
              )}
              {aiOpen && (
                <div className="space-y-3 rounded-lg border border-t_think/20 bg-ink-900/60 p-4">
                  <label className="block text-[10.5px] font-medium uppercase tracking-wider text-fg-faint">Describe your metric</label>
                  <textarea
                    autoFocus
                    value={aiText}
                    onChange={(e) => setAiText(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && aiText.trim()) void runGenerate(); }}
                    rows={4}
                    placeholder="e.g., Check if the assistant's response is helpful and addresses the user's question. I want to evaluate accuracy, completeness, and tone."
                    className="w-full resize-y rounded-lg border border-line bg-ink-900/80 px-3 py-2 text-[12.5px] text-fg placeholder:text-fg-faint/60 focus:border-t_think/40 focus:outline-none"
                  />
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] text-fg-faint/60">⌘ Enter to generate</span>
                    <div className="flex items-center gap-2">
                      <button onClick={() => setAiOpen(false)} className="px-3 py-1.5 text-[11.5px] text-fg-faint transition-colors hover:text-fg-muted">
                        Cancel
                      </button>
                      <button
                        onClick={() => void runGenerate()}
                        disabled={aiBusy || !aiText.trim()}
                        className="inline-flex items-center gap-1.5 rounded-lg bg-t_think px-3.5 py-1.5 text-[12px] font-medium text-fg transition-colors hover:bg-t_think disabled:opacity-50"
                      >
                        {aiBusy ? (
                          <span className="h-3 w-3 animate-spin rounded-full border-2 border-t_think/40 border-t-white" />
                        ) : (
                          <SparklesIcon className="h-3.5 w-3.5" />
                        )}
                        Generate
                      </button>
                    </div>
                  </div>
                </div>
              )}
              {error && <p className="text-[12px] text-fail">{error}</p>}
            </div>
          )}

          {step === "library" && (
            <div className="space-y-4">
              {templates === null ? (
                <div className="flex items-center gap-2 py-8 text-[12.5px] text-fg-faint">
                  <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-line border-t-fg-faint" />
                  Loading library…
                </div>
              ) : (
                grouped.map((g) => {
                  // Use the level color for the group header dot
                  const glc = g.key === "C" ? LEVEL_COLORS.CONVERSATION : g.key === "M" ? LEVEL_COLORS.AGENT_RUN : LEVEL_COLORS.SPAN;
                  return (
                    <div key={g.key} className="space-y-1.5">
                      <div className="flex items-center gap-2 text-[10px] font-medium uppercase tracking-wider text-fg-faint">
                        <span className={clsx("h-1.5 w-1.5 rounded-full", glc.dot)} />
                        {g.label}
                      </div>
                      {g.items.map((t) => (
                        <button
                          key={t.score_name}
                          onClick={() => pickTemplate(t)}
                          disabled={t.installed}
                          className={clsx(
                            "flex w-full items-start justify-between gap-3 rounded-lg border p-3 text-left transition-all",
                            t.installed
                              ? "cursor-default border-line-soft bg-ink-700/30 opacity-50"
                              : "border-line bg-ink-700/30 hover:border-line-bright hover:bg-ink-700/50",
                          )}
                        >
                          <span className="min-w-0">
                            <span className="block truncate text-[12.5px] font-medium text-fg">{t.name}</span>
                            <span className="mt-0.5 block text-[11px] leading-snug text-fg-faint">{t.description}</span>
                          </span>
                          <span className="flex shrink-0 items-center gap-1.5">
                            {t.config?.output_type === "json" && (
                              <span className="rounded border border-info/30 bg-info/10 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wider text-info">Schema</span>
                            )}
                            {t.kind === "llm_judge" && (
                              <span className="rounded border border-t_think/30 bg-t_think/10 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wider text-t_think">LLM</span>
                            )}
                            {t.installed && (
                              <span className="rounded border border-ok/30 bg-ok/10 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wider text-ok">Installed</span>
                            )}
                          </span>
                        </button>
                      ))}
                    </div>
                  );
                })
              )}
              <div className="sticky bottom-0 z-10 -mx-5 -mb-5 mt-1 flex justify-start border-t border-line bg-ink-900/95 px-5 py-3 backdrop-blur-sm">
                <button onClick={() => setStep("type")} className="rounded-lg border border-line px-3.5 py-1.5 text-[12px] text-fg-faint transition-colors hover:border-line-bright hover:text-fg-muted">
                  Back
                </button>
              </div>
            </div>
          )}

          {step === "config" && (
            <div className="space-y-5">
              {/* Level selector — color-coded to match table's level badge system */}
              <div>
                <label className="mb-2 block text-[10.5px] font-medium uppercase tracking-wider text-fg-faint">Evaluation Level</label>
                <div className="grid grid-cols-3 gap-2">
                  {LEVEL_OPTIONS.map((o) => {
                    const lc = LEVEL_COLORS[o.value] ?? LEVEL_COLORS.AGENT_RUN;
                    const active = levelSegment === o.value;
                    return (
                      <button
                        key={o.value}
                        onClick={() =>
                          setForm((f) => {
                            if (o.value === "SPAN" && S_LEVELS.has(f.level)) return f;
                            // dropping cross-group deps: conversation and trace-level metrics
                            // run in separate passes, so deps can't cross that boundary
                            const nextIsConv = o.value === "CONVERSATION";
                            const byName = new Map((allEvaluators ?? []).map((e) => [e.score_name, e]));
                            const dependsOn = f.dependsOn.filter((n) => {
                              const dep = byName.get(n);
                              return dep ? (dep.level === "CONVERSATION") === nextIsConv : false;
                            });
                            return { ...f, level: o.value, dependsOn };
                          })
                        }
                        title={o.hint}
                        className={clsx(
                          "flex items-center justify-center gap-1.5 rounded-lg border px-3 py-2 text-[12px] font-medium transition-all",
                          active
                            ? lc.active
                            : "border-line bg-ink-700/40 text-fg-faint hover:border-line-bright hover:text-fg-muted",
                        )}
                      >
                        {active && <span className={clsx("h-1.5 w-1.5 rounded-full", lc.dot)} />}
                        {o.label}
                      </button>
                    );
                  })}
                </div>
                {S_LEVELS.has(form.level) && form.level !== "SPAN" && (
                  <p className="mt-1.5 text-[10.5px] text-fg-faint">
                    Scoped to <span className="font-mono text-fg-faint">{form.level}</span> steps only.
                  </p>
                )}
                {form.kind === "llm_judge" && levelSegment === "SPAN" && (
                  <div className="mt-3">
                    <label className="mb-2 block text-[10.5px] font-medium uppercase tracking-wider text-fg-faint">
                      Step types to grade
                    </label>
                    <div className="grid grid-cols-4 gap-2">
                      {SPAN_TYPE_OPTIONS.map((o) => {
                        const active = form.spanTypes.includes(o.value);
                        return (
                          <button
                            key={o.value}
                            type="button"
                            onClick={() =>
                              setForm((f) => ({
                                ...f,
                                level: "SPAN", // span_types only applies at SPAN level
                                spanTypes: active
                                  ? f.spanTypes.filter((t) => t !== o.value)
                                  : [...f.spanTypes, o.value],
                              }))
                            }
                            className={clsx(
                              "flex items-center justify-center gap-1.5 rounded-lg border px-2 py-2 text-[12px] font-medium transition-all",
                              active
                                ? levelColors.active
                                : "border-line bg-ink-700/40 text-fg-faint hover:border-line-bright hover:text-fg-muted",
                            )}
                          >
                            {active && <span className={clsx("h-1.5 w-1.5 rounded-full", levelColors.dot)} />}
                            {o.label}
                          </button>
                        );
                      })}
                    </div>
                    <p className="mt-1.5 text-[10.5px] text-fg-faint">
                      The judge runs once per matching step. Pick only <span className="font-mono">Tool</span> to grade tool calls exclusively.
                    </p>
                  </div>
                )}
              </div>

              {/* Name + description */}
              <div className="space-y-3">
                <div>
                  <label className="mb-1.5 block text-[10.5px] font-medium uppercase tracking-wider text-fg-faint">Metric Name</label>
                  <input
                    value={form.name}
                    onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                    placeholder="e.g., Helpfulness Score"
                    className="w-full rounded-lg border border-line bg-ink-900/60 px-3 py-2 text-[12.5px] text-fg placeholder:text-fg-faint/60 focus:border-signal/40 focus:outline-none"
                  />
                </div>

                <div>
                  <label className="mb-1.5 flex items-center gap-1.5 text-[10.5px] font-medium uppercase tracking-wider text-fg-faint">
                    Description
                    <span className="rounded bg-ink-700 px-1 py-0.5 text-[9px] font-normal normal-case tracking-normal text-fg-faint">optional</span>
                  </label>
                  <input
                    value={form.description}
                    onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
                    placeholder="Brief description of what this metric evaluates"
                    className="w-full rounded-lg border border-line bg-ink-900/60 px-3 py-2 text-[12.5px] text-fg placeholder:text-fg-faint/60 focus:border-signal/40 focus:outline-none"
                  />
                </div>
              </div>

              {/* Separator */}
              <div className="border-t border-line" />

              {isStructural ? (
                <div>
                  <label className="mb-1.5 block text-[10.5px] font-medium uppercase tracking-wider text-fg-faint">
                    Parameters
                    <span className="ml-1.5 font-mono normal-case tracking-normal text-fg-faint">({editing?.config?.check ?? form.scoreName ?? "built-in"})</span>
                  </label>
                  <textarea
                    value={form.paramsJson}
                    onChange={(e) => setForm((f) => ({ ...f, paramsJson: e.target.value }))}
                    rows={4}
                    spellCheck={false}
                    className="w-full resize-y rounded-lg border border-line bg-ink-900/60 px-3 py-2 font-mono text-[12px] text-fg focus:border-signal/40 focus:outline-none"
                  />
                </div>
              ) : (
                <>
                  <div>
                    <div className="mb-1.5 flex items-center justify-between">
                      <label className="block text-[10.5px] font-medium uppercase tracking-wider text-fg-faint">
                        {isDecisionCol ? (promptMode === "basic" ? "What the model reads" : "State template (what the model reads)") : "Evaluation Prompt"}
                      </label>
                      <div className="flex items-center gap-0.5 rounded-md border border-line bg-ink-900/60 p-0.5">
                        {(["basic", "advanced"] as const).map((m) => (
                          <button
                            key={m}
                            type="button"
                            onClick={() => setPromptMode(m)}
                            className={clsx(
                              "rounded px-2.5 py-0.5 text-[10.5px] font-medium capitalize transition-colors",
                              promptMode === m
                                ? "bg-ok/15 text-ok"
                                : "text-fg-faint hover:text-fg-muted",
                            )}
                          >
                            {m}
                          </button>
                        ))}
                      </div>
                    </div>
                    {promptMode === "basic" && isDecisionCol ? (
                      <p className="rounded-lg border border-line bg-ink-900/40 px-3 py-2 text-[11px] leading-relaxed text-fg-faint">
                        The item itself — the request and answer, the transcript, or the step I/O for this level — is sent
                        as the model&apos;s state. Switch to <span className="text-fg-muted">Advanced</span> to choose exactly
                        what it reads with <span className="font-mono">@VARIABLES</span>; less irrelevant context means
                        more accurate answers.
                      </p>
                    ) : promptMode === "basic" ? (
                      <>
                        <textarea
                          value={form.prompt}
                          onChange={(e) => setForm((f) => ({ ...f, prompt: e.target.value }))}
                          rows={5}
                          placeholder="You are evaluating the quality of an AI assistant's response. Consider…"
                          className="w-full resize-y rounded-lg border border-line bg-ink-900/60 px-3 py-2 text-[12.5px] leading-relaxed text-fg placeholder:text-fg-faint/60 focus:border-signal/40 focus:outline-none"
                        />
                        <p className="mt-1.5 text-[10.5px] text-fg-faint">
                          Trace content (request, answer, tool results / transcript / step I/O) is appended automatically.
                        </p>
                      </>
                    ) : (
                      <div className="space-y-2.5">
                        <AdvancedPromptEditor
                          value={form.prompt}
                          onChange={(v) => setForm((f) => ({ ...f, prompt: v }))}
                          level={form.level}
                          sequential={form.executionMode === "sequential"}
                          placeholder="Grade the answer. Use @HISTORY, @GOAL, @CURRENT_STEP.tool_call, … (type @ for variables)"
                        />
                        {form.executionMode === "sequential" && (
                          <p className="text-[10.5px] text-fg-faint">
                            Tip: use <span className="font-mono text-ok">@METRIC_PREVIOUS_RESULT</span> to read the previous evaluation result.
                          </p>
                        )}
                        <PromptPreview prompt={form.prompt} level={form.level} defaultThread={previewThread} />
                      </div>
                    )}
                  </div>

                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="mb-1.5 block text-[10.5px] font-medium uppercase tracking-wider text-fg-faint">Model</label>
                      <select
                        value={form.model}
                        onChange={(e) => pickModel(e.target.value)}
                        className="w-full rounded-lg border border-line bg-ink-900/60 px-3 py-2 text-[12.5px] text-fg focus:border-signal/40 focus:outline-none"
                      >
                        <option value="">{defaultModelLabel}</option>
                        <optgroup label="LLM judges">
                          {modelOptions.filter((m) => m.kind !== "decision").map((m) => (
                            <option key={m.id} value={m.id}>
                              {m.label}{m.context_tokens ? ` · ${fmtContext(m.context_tokens)}` : ""}
                            </option>
                          ))}
                        </optgroup>
                        {modelOptions.some((m) => m.kind === "decision") && (
                          <optgroup label="Decision models (classifiers)">
                            {modelOptions.filter((m) => m.kind === "decision").map((m) => (
                              <option key={m.id} value={m.id}>
                                {m.label}{m.context_tokens ? ` · ${fmtContext(m.context_tokens)}` : ""}
                              </option>
                            ))}
                          </optgroup>
                        )}
                        {form.model && !modelOptions.some((m) => m.id === form.model) && (
                          <option value={form.model}>{form.model}</option>
                        )}
                      </select>
                    </div>
                    <div>
                      <label className="mb-1.5 block text-[10.5px] font-medium uppercase tracking-wider text-fg-faint">
                        {isDecisionCol ? "Question Type" : "Output Type"}
                      </label>
                      <select
                        value={form.outputType}
                        onChange={(e) => {
                          const next = e.target.value as FormState["outputType"];
                          setForm((f) => ({
                            ...f,
                            outputType: next,
                            ...(isDecisionOutput(next) && next !== f.outputType ? { threshold: "0.5" } : {}),
                          }));
                        }}
                        className="w-full rounded-lg border border-line bg-ink-900/60 px-3 py-2 text-[12.5px] text-fg focus:border-signal/40 focus:outline-none"
                      >
                        {OUTPUT_OPTIONS.filter((o) =>
                          decisionModel || isDecisionCol ? isDecisionOutput(o.value) : LLM_OUTPUT_VALUES.has(o.value),
                        ).map((o) => (
                          <option key={o.value} value={o.value}>{o.label}</option>
                        ))}
                      </select>
                    </div>
                  </div>

                  <div>
                    <label className="mb-1.5 block text-[10.5px] font-medium uppercase tracking-wider text-fg-faint">
                      If an item exceeds the context{" "}
                      <span className="font-normal normal-case tracking-normal text-fg-faint">
                        {primaryInfo?.context_tokens ? `(${fmtContext(primaryInfo.context_tokens)} tokens)` : ""}
                      </span>
                    </label>
                    <select
                      value={form.fallbackModel}
                      onChange={(e) => setForm((f) => ({ ...f, fallbackModel: e.target.value }))}
                      className="w-full rounded-lg border border-line bg-ink-900/60 px-3 py-2 text-[12.5px] text-fg focus:border-signal/40 focus:outline-none"
                    >
                      <option value="">Skip it (no verdict, marked &ldquo;input too long&rdquo;)</option>
                      {fallbackOptions.map((m) => (
                        <option key={m.id} value={m.id}>
                          Grade with {m.label}{m.context_tokens ? ` · ${fmtContext(m.context_tokens)}` : ""}
                        </option>
                      ))}
                      {form.fallbackModel && !fallbackOptions.some((m) => m.id === form.fallbackModel) && (
                        <option value={form.fallbackModel}>Grade with {form.fallbackModel}</option>
                      )}
                    </select>
                    <p className="mt-1.5 text-[10.5px] text-fg-faint">
                      Nothing is truncated. {isDecisionCol
                        ? "The fallback LLM answers the same question with the same options, so the column keeps one score shape."
                        : "Items that fit are always graded by the column's own model."}
                    </p>
                  </div>

                  {isDecisionCol && (
                    <DecisionQuestionEditor
                      type={form.outputType as Parameters<typeof decisionProblem>[0]}
                      value={form.decision}
                      onChange={(d) => setForm((f) => ({ ...f, decision: d }))}
                      threshold={form.threshold}
                      onThreshold={(t) => setForm((f) => ({ ...f, threshold: t }))}
                    />
                  )}

                  {(form.outputType === "score" || form.outputType === "number" || form.outputType === "json") && (
                    <div>
                      <label className="mb-1.5 block text-[10.5px] font-medium uppercase tracking-wider text-fg-faint">
                        Pass threshold{" "}
                        <span className="font-normal normal-case tracking-normal text-fg-faint">
                          {form.outputType === "number"
                            ? "(PASS when ≥ this value)"
                            : form.outputType === "json"
                              ? "(0–1, on your score field)"
                              : "(0–1)"}
                        </span>
                      </label>
                      <input
                        value={form.threshold}
                        onChange={(e) => setForm((f) => ({ ...f, threshold: e.target.value }))}
                        inputMode="decimal"
                        placeholder="none"
                        title="Leave empty for an informational metric (no PASS/FAIL)"
                        className="w-32 rounded-lg border border-line bg-ink-900/60 px-3 py-2 font-mono text-[12px] text-fg placeholder:text-fg-faint/60 focus:border-signal/40 focus:outline-none"
                      />
                    </div>
                  )}

                  <div>
                    <label className="mb-2 block text-[10.5px] font-medium uppercase tracking-wider text-fg-faint">Execution Mode</label>
                    <div className="grid grid-cols-2 gap-3">
                      {(["batch", "sequential"] as const).map((mode) => (
                        <button
                          key={mode}
                          type="button"
                          onClick={() => setForm((f) => ({ ...f, executionMode: mode }))}
                          className={clsx(
                            "flex flex-col items-center rounded-lg border px-3 py-2.5 transition-all",
                            form.executionMode === mode
                              ? levelColors.active
                              : "border-line bg-ink-700/40 text-fg-faint hover:border-line-bright hover:text-fg-muted",
                          )}
                        >
                          <span className="text-[12px] font-semibold capitalize">{mode}</span>
                          <span className="mt-0.5 text-[10px] text-current opacity-60">
                            {mode === "batch" ? "Independent evaluation" : "Chain results"}
                          </span>
                        </button>
                      ))}
                    </div>
                    {form.executionMode === "sequential" && (
                      <p className="mt-1.5 text-[10.5px] text-fg-faint">
                        {form.level === "CONVERSATION"
                          ? "No effect at conversation level — single grade per thread."
                          : isDecisionCol
                            ? "The earlier turns (or earlier steps) and this column's previous result are added to what the model reads."
                            : "Steps chain within each run; message grades chain across the conversation after it settles."}
                      </p>
                    )}
                  </div>

                  {/* Depends On — only useful for llm_judge; structural evals have no prompt to inject into.
                      Restricted to the same dispatch group: conversation-level metrics run as a separate
                      pass from trace/step-level ones, so a cross-group dependency would never see its
                      prerequisite's result. */}
                  {(() => {
                    const isConv = form.level === "CONVERSATION";
                    const candidates = (allEvaluators ?? []).filter(
                      (e) =>
                        e.kind === "llm_judge" &&
                        e.id !== editing?.id &&
                        (e.level === "CONVERSATION") === isConv,
                    );
                    if (candidates.length === 0) return null;
                    return (
                      <div>
                        <label className="mb-1 block text-[10.5px] font-medium uppercase tracking-wider text-fg-faint">
                          Depends On{" "}
                          <span className="rounded bg-ink-700 px-1 py-0.5 text-[9px] font-normal normal-case tracking-normal text-fg-faint">optional</span>
                        </label>
                        <p className="mb-2 text-[10.5px] text-fg-faint">
                          Run after these metrics and inject their results as context into the evaluation prompt.
                        </p>
                        <div className="space-y-1 rounded-lg border border-line bg-ink-900/40 p-2">
                          {candidates.map((e) => {
                            const checked = form.dependsOn.includes(e.score_name);
                            const lc =
                              e.level === "CONVERSATION"
                                ? LEVEL_COLORS.CONVERSATION
                                : e.level === "AGENT_RUN"
                                  ? LEVEL_COLORS.AGENT_RUN
                                  : LEVEL_COLORS.SPAN;
                            return (
                              <label
                                key={e.id}
                                className={clsx(
                                  "flex cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-1.5 transition-colors",
                                  checked ? "bg-ink-700/60" : "hover:bg-ink-700/30",
                                )}
                              >
                                <input
                                  type="checkbox"
                                  checked={checked}
                                  onChange={() =>
                                    setForm((f) => ({
                                      ...f,
                                      dependsOn: checked
                                        ? f.dependsOn.filter((n) => n !== e.score_name)
                                        : [...f.dependsOn, e.score_name],
                                      // unchecking a column drops the conditions that read it
                                      runIf: checked ? f.runIf.filter((c) => c.column !== e.score_name) : f.runIf,
                                    }))
                                  }
                                  className="accent-signal"
                                />
                                <span className="min-w-0 flex-1 truncate text-[12px] text-fg-muted">{e.name}</span>
                                <span className={clsx("shrink-0 rounded px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wider border", lc.active)}>
                                  {e.level === "CONVERSATION" ? "Conv" : e.level === "AGENT_RUN" ? "Msg" : "Step"}
                                </span>
                              </label>
                            );
                          })}
                        </div>
                        <div className="mt-3">
                          <RunIfEditor
                            candidates={candidates}
                            value={form.runIf}
                            onChange={(runIf) => setForm((f) => ({
                              ...f,
                              runIf,
                              // a column a condition reads is checked in Depends On too
                              dependsOn: [...new Set([...f.dependsOn, ...runIf.map((c) => c.column)])],
                            }))}
                          />
                        </div>
                      </div>
                    );
                  })()}
                </>
              )}

              <label className="flex cursor-pointer items-center gap-2.5 rounded-lg border border-line bg-ink-700/40 px-3 py-2.5 text-[12px] text-fg-muted transition-colors hover:border-line-bright hover:text-fg-muted">
                <input
                  type="checkbox"
                  checked={form.enabled}
                  onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))}
                  className="accent-signal"
                />
                Run automatically on new traces
              </label>

              {error && <p className="text-[12px] text-fail">{error}</p>}

              <div className="sticky bottom-0 z-10 -mx-5 -mb-5 mt-1 flex items-center justify-between border-t border-line bg-ink-900/95 px-5 py-3 backdrop-blur-sm">
                <button
                  onClick={() => (editing ? requestClose() : setStep("type"))}
                  className="rounded-lg border border-line px-3.5 py-1.5 text-[12px] text-fg-faint transition-colors hover:border-line-bright hover:text-fg-muted"
                >
                  {editing ? "Cancel" : "Back"}
                </button>
                <button
                  onClick={submitConfig}
                  disabled={busy}
                  className="btn-primary"
                >
                  {busy && <span className="h-3 w-3 animate-spin rounded-full border-2 border-info/40 border-t-white" />}
                  {form.kind === "llm_judge" && form.outputType === "json"
                    ? (editing ? "Next: Edit Schema" : "Next: Define Schema")
                    : saveLabel}
                </button>
              </div>
            </div>
          )}

          {step === "schema" && (
            <div className="space-y-4">
              <OutputSchemaBuilder
                schema={form.outputSchema}
                onChange={(schema) => setForm((f) => ({ ...f, outputSchema: schema }))}
              />

              {error && <p className="text-[12px] text-fail">{error}</p>}

              <div className="sticky bottom-0 z-10 -mx-5 -mb-5 mt-1 flex items-center justify-between border-t border-line bg-ink-900/95 px-5 py-3 backdrop-blur-sm">
                <button
                  onClick={() => setStep("config")}
                  className="rounded-lg border border-line px-3.5 py-1.5 text-[12px] text-fg-faint transition-colors hover:border-line-bright hover:text-fg-muted"
                >
                  Back
                </button>
                <button
                  onClick={() => void save()}
                  disabled={busy}
                  className="btn-primary"
                >
                  {busy && <span className="h-3 w-3 animate-spin rounded-full border-2 border-info/40 border-t-white" />}
                  {saveLabel}
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
