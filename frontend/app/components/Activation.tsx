import { CopyId } from "./CopyId";
import { PipelinePeek } from "./PipelinePeek";
import { SampleButton } from "./SampleButton";
import type { Milestone } from "@/app/lib/api";

/* The first-run path, told as the product's own loop: trace → check → confirmed failure →
   reproduced → verified fix → CI. Every step's done-ness is a DURABLE milestone the backend
   recorded from a confirmed operation (services/milestones.py) — never a click, never sample
   data. Workspaces from before milestones existed fall back to the old counts. Only the first
   unfinished step is expanded; the rest are one line each. */

export type ActivationState = {
  traces: number;
  evaluators: number;
  failures: number;
  clusters: number;
  cases: number;
  gates: number;
  ingestKey: string;
  endpoint: string;
  milestones: Milestone[];
};

type Step = {
  title: string;
  done: boolean;
  /** Shown on the right of a finished step — the evidence it's done. */
  proof: string;
  /** What to do about it, rendered only while this is the current step. */
  body: React.ReactNode;
};

const n = (v: number) => v.toLocaleString("en-US");
const when = (iso: string | null) => (iso ? iso.slice(0, 16).replace("T", " ") : "");

function Snippet({ code, label }: { code: string; label: string }) {
  return (
    <div className="mt-2 overflow-hidden rounded-lg border border-line bg-ink-900">
      <div className="flex items-center justify-between border-b border-line/60 px-3 py-1.5">
        <span className="font-mono text-[10px] uppercase tracking-wider text-fg-faint">{label}</span>
        <CopyId value={code} text="copy" label={label} />
      </div>
      <pre className="overflow-x-auto p-3 font-mono text-[11.5px] leading-relaxed text-fg-muted">{code}</pre>
    </div>
  );
}

function Action({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a href={href} className="btn-ghost mt-2 inline-flex">
      {children}
    </a>
  );
}

export function Activation(s: ActivationState) {
  const ms = s.milestones ?? [];
  const real = (name: string) => ms.find((m) => m.name === name && m.first_at && !m.sample) ?? null;
  const sampleOnly = ms.some((m) => m.first_at && m.sample) && !ms.some((m) => m.first_at && !m.sample);
  // Pre-milestone workspaces: nothing was ever recorded, so the old count-derived doneness stands.
  const legacy = !ms.some((m) => m.first_at);
  const done = (name: string, fallback: boolean) => Boolean(real(name)) || (legacy && fallback);
  const proof = (name: string, fallback: string) => {
    const m = real(name);
    return m ? `${when(m.first_at)}${m.integration ? ` · ${m.integration}` : ""}` : fallback;
  };

  const steps: Step[] = [
    {
      title: "Send your first trace",
      done: done("first_trace_received", s.traces > 0),
      proof: proof("first_trace_received", `${n(s.traces)} traces`),
      body: (
        <>
          <p className="text-[12.5px] text-fg-muted">
            Point the SDK at this workspace. Auto-instrumentation traces your existing agent with
            no span code. The package is <code className="font-mono">tracely-ai</code>.
          </p>
          <Snippet
            label="python"
            code={`pip install "tracely-ai[openai]"

import tracely_sdk as tracely
tracely.init(endpoint="${s.endpoint}", api_key="${s.ingestKey}")

with tracely.trace(agent="support", conversation="conv-1"):
    ...  # your agent runs as usual`}
          />
        </>
      ),
    },
    {
      title: "Complete a check on a real run",
      done: done("first_check_completed", s.evaluators > 0 && s.traces > 0),
      proof: proof("first_check_completed", `${n(s.evaluators)} evaluator${s.evaluators === 1 ? "" : "s"}`),
      body: (
        <>
          <p className="text-[12.5px] text-fg-muted">
            Evaluators are the columns of the traces table — structural checks run free, an LLM
            judge grades the answer. This ticks when one actually produces a verdict on your trace.
          </p>
          <Action href="/traces">Add a column on Traces →</Action>
        </>
      ),
    },
    {
      title: "Confirm a real failure as a test",
      done: done("source_failure_confirmed", s.cases > 0),
      proof: proof("source_failure_confirmed", `${n(s.cases)} case${s.cases === 1 ? "" : "s"}`),
      body: (
        <>
          <p className="text-[12.5px] text-fg-muted">
            Promote a failing run. It becomes a case only once the original is shown to fail the
            contract — a failure that cannot be told from a fix is left as a draft.
          </p>
          <Action href="/clusters">Promote from a failure cluster →</Action>
        </>
      ),
    },
    {
      title: "Reproduce it under recorded conditions",
      done: done("case_reproduced", false),
      proof: proof("case_reproduced", ""),
      body: (
        <>
          <p className="text-[12.5px] text-fg-muted">
            Replay the case against your current code with the recorded tools and model. A FAIL
            here is the bug reproduced offline — no keys, no cost.
          </p>
          <Snippet
            label="shell"
            code={`pip install tracely-ai
TRACELY_API=${s.endpoint} TRACELY_KEY=${s.ingestKey} \\
  tracely replay --agent support --entrypoint app.agent:run`}
          />
        </>
      ),
    },
    {
      title: "Verify a fix",
      done: done("candidate_verified", false),
      proof: proof("candidate_verified", ""),
      body: (
        <p className="text-[12.5px] text-fg-muted">
          Fix the agent, run the same command (or pick the run on the case page and press Verify).
          The case records which candidate passed, at which case version.
        </p>
      ),
    },
    {
      title: "Run it in CI",
      done: done("ci_check_completed", s.gates > 0),
      proof: proof("ci_check_completed", `${n(s.gates)} gate run${s.gates === 1 ? "" : "s"}`),
      body: (
        <>
          <p className="text-[12.5px] text-fg-muted">
            Add the gate to your workflow. It exits non-zero and posts a commit status; this ticks
            when a run-scoped gate actually grades the suite against a revision.
          </p>
          <Action href="https://docs.tracely-ai.com/cli">CI setup →</Action>
        </>
      ),
    },
  ];

  const finished = steps.filter((x) => x.done).length;
  if (finished === steps.length) return null; // the loop is closed — this card has nothing left to say
  const current = steps.findIndex((x) => !x.done);

  return (
    <section className="reveal card p-5" aria-label="Getting started">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-[15px] font-semibold text-fg">Close the loop once</h2>
          <p className="mt-0.5 text-[12.5px] text-fg-muted">
            Six real outcomes, in order. Each ticks from the operation itself — not from a click.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span className="font-mono text-[12px] text-fg-faint">{finished} / {steps.length}</span>
          <SampleButton />
          <a href="#connect" className="btn-primary">Connect my agent</a>
        </div>
      </div>
      {sampleOnly && (
        <p className="mt-3 rounded-md border border-warn/30 bg-warn/[0.05] px-3 py-2 text-[12px] text-warn">
          Sample data is flowing (labelled <code className="font-mono">sample</code> everywhere). It never ticks these steps —
          they are yours to earn with your own agent.
        </p>
      )}
      <PipelinePeek live={{ traces: s.traces, evaluators: s.evaluators, failures: s.failures, clusters: s.clusters, cases: s.cases, gates: s.gates }} />
      <ol id="connect" className="mt-4 space-y-2">
        {steps.map((step, i) => (
          <li key={i} className="rounded-lg border border-line/60 px-3 py-2">
            <div className="flex items-center gap-2.5">
              <span
                className={`grid h-4 w-4 shrink-0 place-items-center rounded-full border text-[9px] ${
                  step.done ? "border-ok/50 bg-ok/15 text-ok" : i === current ? "border-signal/60 bg-signal/15 text-signal" : "border-line text-fg-faint"
                }`}
              >
                {step.done ? "✓" : i + 1}
              </span>
              <span
                className={`flex-1 truncate text-[13px] ${
                  step.done ? "text-fg-muted line-through decoration-line" : i === current ? "font-semibold text-fg" : "text-fg-muted"
                }`}
              >
                {step.title}
              </span>
              {step.done && step.proof && <span className="shrink-0 font-mono text-[10.5px] text-fg-faint">{step.proof}</span>}
            </div>
            {i === current && <div className="mt-2 pl-6.5">{step.body}</div>}
          </li>
        ))}
      </ol>
    </section>
  );
}
