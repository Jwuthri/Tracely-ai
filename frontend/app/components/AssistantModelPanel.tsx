"use client";

import { useEffect, useState } from "react";

type Model = { id: string; label: string };
type Config = { model: string; default: string; custom: boolean; models: Model[] };

/** Settings -> API keys: which OpenRouter model the in-app assistant answers on in this
 *  workspace. "Default" follows the server's ASSISTANT_MODEL; anything else is pinned here. */
export function AssistantModelPanel() {
  const [cfg, setCfg] = useState<Config | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    fetch("/api/project/assistant-model", { cache: "no-store" })
      .then((r) => r.json())
      .then((d) => d?.default && setCfg(d))
      .catch(() => setMsg({ ok: false, text: "Could not load the assistant model." }));
  }, []);

  async function choose(value: string) {
    setBusy(true);
    setMsg(null);
    try {
      const r = await fetch("/api/project/assistant-model", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ model: value || null }),
      });
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        setMsg({ ok: false, text: d?.detail ?? `Save failed (HTTP ${r.status})` });
        return;
      }
      setCfg(d);
      setMsg({ ok: true, text: "Saved. The next message uses it." });
    } catch {
      setMsg({ ok: false, text: "Save failed: could not reach the server." });
    } finally {
      setBusy(false);
    }
  }

  const label = (id: string) => cfg?.models.find((m) => m.id === id)?.label ?? id;

  return (
    <section className="card overflow-hidden">
      <div className="hairline px-4 py-3 text-[13px] font-semibold text-fg">Assistant model</div>
      <div className="space-y-4 p-5">
        <p className="text-[13.5px] leading-relaxed text-fg-muted">
          The model the in-app assistant answers on. It runs on this deployment's own key when it
          has one, otherwise on the workspace key above.
        </p>
        {!cfg && !msg && <p className="text-[13px] text-fg-faint">Loading…</p>}
        {cfg && (
          <select
            aria-label="Assistant model"
            value={cfg.custom ? cfg.model : ""}
            disabled={busy}
            onChange={(e) => choose(e.target.value)}
            className="w-80 max-w-full rounded-lg border border-line bg-ink-900 px-3 py-2 text-[13px] text-fg focus:border-signal/50 focus:outline-none disabled:opacity-40"
          >
            <option value="">Default ({label(cfg.default)})</option>
            {cfg.models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
          </select>
        )}
        {msg && (
          <p role={msg.ok ? "status" : "alert"} className={`text-[12.5px] ${msg.ok ? "text-ok" : "text-fail"}`}>
            {msg.text}
          </p>
        )}
      </div>
    </section>
  );
}
