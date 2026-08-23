// What the page the user is looking at wants the assistant to know, and how it hears back.
//
// The chat widget lives in the (app) layout; a page that has unsaved state worth sharing (the
// alert editor's rule) registers a GETTER here, and the widget calls it at send time — so the
// model sees the canvas as it is when the message goes, not as it was when the page mounted.
// ponytail: a module singleton, not a React context. One page registers; lift it if a second
// page ever needs to share state AND they can be mounted together.

let getter: (() => unknown) | null = null;

export function setPageContext(get: (() => unknown) | null) {
  getter = get;
}

export function readPageContext(): unknown {
  try {
    return getter?.() ?? null;
  } catch {
    return null;
  }
}

/** Fired by the chat when `draft_alert` succeeded; `detail` is the tool's arguments. */
export const ALERT_DRAFT_EVENT = "tracely:alert-draft";
/** Fired by any page to open the chat, optionally with a prompt typed into the composer. */
export const ASSISTANT_OPEN_EVENT = "tracely:assistant-open";

export type AlertDraftArgs = {
  name: string;
  trigger: string;
  steps: { name?: string; step_type: string; config?: Record<string, unknown> }[];
  description?: string;
  target_agent?: string;
  contains?: string;
  score_name?: string;
  env?: string;
  threshold?: number;
  window_minutes?: number;
  min_samples?: number;
};

export function openAssistant(prompt = "") {
  window.dispatchEvent(new CustomEvent(ASSISTANT_OPEN_EVENT, { detail: { prompt } }));
}
