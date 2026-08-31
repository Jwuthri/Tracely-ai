import type { SpanOut } from "./api";

const PARAM_PREFIX = "gen_ai.request.";
const META_PREFIX = "tracely.metadata.";

// metadata values arrive stringified; coerce numbers / bools / JSON back for clean display.
function coerce(v: string): unknown {
  if (v === "true") return true;
  if (v === "false") return false;
  const t = v.trim();
  // only coerce when the number round-trips exactly — `"0098231"`, `"1.0"`, `"0x10"`, `"1e3"` are
  // ids/versions the user set, and rewriting them breaks both display and the search haystack.
  if (t !== "" && String(Number(t)) === t) return Number(t);
  if (t.startsWith("{") || t.startsWith("[")) {
    try {
      return JSON.parse(t);
    } catch {
      /* not JSON */
    }
  }
  return v;
}

// Clean, display-worthy metadata for a span: LLM sampling params (temperature, top_p, max_tokens,
// …) plus any user-attached `tracely.metadata.*` — excluding the noisy raw OTel/instrumentation
// attributes and the I/O (which have their own columns).
export function spanMeta(span: SpanOut): Record<string, unknown> {
  const md = span.metadata || {};
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(md)) {
    if (k.startsWith(PARAM_PREFIX) && k !== `${PARAM_PREFIX}model`) {
      out[k.slice(PARAM_PREFIX.length)] = coerce(v);
    } else if (k.startsWith(META_PREFIX)) {
      out[k.slice(META_PREFIX.length)] = coerce(v);
    }
  }
  return out;
}

// ONLY user-set metadata (tracely.metadata.*), prefix stripped — this is the "metadata" a user
// chooses to attach (prompt version, tenant, user id, …), shown at the conversation level and used
// for filtering. The LLM sampling params (gen_ai.request.*) are intentionally excluded here.
function customMeta(span: SpanOut): Record<string, unknown> {
  const md = span.metadata || {};
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(md)) {
    if (k.startsWith(META_PREFIX)) out[k.slice(META_PREFIX.length)] = coerce(v);
  }
  return out;
}

// Union of user metadata across a set of spans (a turn or a whole conversation); last value wins.
export function mergeMeta(spans: SpanOut[] | undefined): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const s of spans ?? []) Object.assign(out, customMeta(s));
  return out;
}

// Flattened "key:value key:value" text for client-side filtering/search.
export function metaText(meta: Record<string, unknown>): string {
  return Object.entries(meta)
    .map(([k, v]) => `${k}:${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
    .join(" ");
}
