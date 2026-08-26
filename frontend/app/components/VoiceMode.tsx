"use client";

import clsx from "clsx";
import { useCallback, useEffect, useRef, useState } from "react";
import { IconMic } from "./icons";
import {
  fetchVoiceProviders,
  startVoiceSession,
  type VoiceHandle,
  type VoiceProviderInfo,
  type VoiceStatus,
} from "@/app/lib/voice";

/* The assistant's speech mode — lives inside the chat panel, swapped in for the transcript
   view. Pick a provider (whichever of OpenAI / Grok this deployment has keys for) and any of
   that provider's voices, press the mic, talk. The voice model answers out loud and reaches the
   workspace through the same text agent as the chat (`askTracely`), so anything it asserts
   about traces went through the regular tools as the regular caller.

   The provider + voice choice is a workspace default (`ui_prefs.voice`) — "set once, whole
   team gets it" — the same mechanism as the hidden-span-type defaults. */

type Line = { role: "user" | "assistant"; text: string };
type VoicePref = { provider?: string; voice?: string };

const STATUS_LABEL: Record<VoiceStatus, string> = {
  connecting: "connecting…",
  listening: "listening",
  speaking: "speaking",
  thinking: "asking Tracely…",
  closed: "tap to talk",
};

export function VoiceMode({ askTracely }: { askTracely: (q: string) => Promise<string> }) {
  const [providers, setProviders] = useState<VoiceProviderInfo[] | null>(null);
  const [providerId, setProviderId] = useState<string>("");
  const [voice, setVoice] = useState<string>("");
  const [status, setStatus] = useState<VoiceStatus>("closed");
  const [lines, setLines] = useState<Line[]>([]);
  const [error, setError] = useState("");
  const live = useRef<VoiceHandle | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const prefsRef = useRef<Record<string, unknown>>({});

  // Providers + the workspace's saved choice, together: the pref only means something once we
  // know which providers exist.
  useEffect(() => {
    let alive = true;
    (async () => {
      const [list, prefs] = await Promise.all([
        fetchVoiceProviders(),
        fetch("/api/project/ui-prefs", { cache: "no-store" })
          .then((r) => (r.ok ? r.json() : { prefs: {} }))
          .then((d) => (d?.prefs ?? {}) as Record<string, unknown>)
          .catch(() => ({}) as Record<string, unknown>),
      ]);
      if (!alive) return;
      prefsRef.current = prefs;
      const saved = (prefs.voice ?? {}) as VoicePref;
      const provider = list.find((p) => p.id === saved.provider) ?? list[0];
      setProviders(list);
      if (provider) {
        setProviderId(provider.id);
        setVoice(
          saved.voice && provider.voices.includes(saved.voice) ? saved.voice : provider.default_voice,
        );
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  // Leaving the view (or the panel) hangs up — a mic left open costs money nobody is talking to.
  useEffect(
    () => () => {
      live.current?.stop();
      live.current = null;
    },
    [],
  );

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [lines, status]);

  const savePref = useCallback((provider: string, v: string) => {
    // Merge into whatever else lives in ui_prefs — a plain PUT would clobber hiddenTypes.
    const prefs = { ...prefsRef.current, voice: { provider, voice: v } };
    prefsRef.current = prefs;
    void fetch("/api/project/ui-prefs", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prefs }),
    }).catch(() => {});
  }, []);

  const provider = providers?.find((p) => p.id === providerId);

  function hangUp() {
    live.current?.stop();
    live.current = null;
    setStatus("closed");
  }

  async function talk() {
    if (live.current) return hangUp();
    if (!provider) return;
    setError("");
    setLines([]);
    try {
      live.current = await startVoiceSession({
        provider: provider.id,
        voice,
        askTracely,
        onEvent: (e) => {
          if (e.type === "status") return setStatus(e.status);
          if (e.type === "error") return setError(e.detail);
          if (e.type === "user_transcript")
            return setLines((l) => [...l, { role: "user", text: e.text }]);
          if (e.type === "assistant_transcript") {
            if (e.final) return;
            return setLines((l) => {
              const last = l[l.length - 1];
              return last?.role === "assistant"
                ? [...l.slice(0, -1), { role: "assistant", text: last.text + e.text }]
                : [...l, { role: "assistant", text: e.text }];
            });
          }
        },
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "couldn't start the voice session");
      setStatus("closed");
    }
  }

  if (providers === null)
    return <p className="px-4 py-8 text-center text-[12px] text-fg-muted">Loading voices…</p>;
  if (providers.length === 0)
    return (
      <p className="px-4 py-8 text-center text-[12px] leading-relaxed text-fg-muted">
        No speech provider is configured on this deployment — set{" "}
        <code className="font-mono text-[11px]">OPENAI_API_KEY</code> or{" "}
        <code className="font-mono text-[11px]">XAI_API_KEY</code> on the backend.
      </p>
    );

  const inCall = status !== "closed";
  const select =
    "h-7 rounded-md border border-line bg-ink-800 px-1.5 font-mono text-[11px] text-fg-muted focus:outline-none disabled:opacity-50";

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      <div className="flex items-center gap-1.5 border-b border-line px-3 py-2">
        <select
          aria-label="Voice provider"
          className={select}
          value={providerId}
          disabled={inCall}
          onChange={(e) => {
            const p = providers.find((x) => x.id === e.target.value);
            if (!p) return;
            setProviderId(p.id);
            setVoice(p.default_voice);
            savePref(p.id, p.default_voice);
          }}
        >
          {providers.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
        </select>
        <select
          aria-label="Voice"
          className={clsx(select, "min-w-0 flex-1")}
          value={voice}
          disabled={inCall}
          onChange={(e) => {
            setVoice(e.target.value);
            savePref(providerId, e.target.value);
          }}
        >
          {provider?.voices.map((v) => (
            <option key={v} value={v}>
              {v}
            </option>
          ))}
        </select>
      </div>

      <div ref={scroller} className="flex-1 space-y-2 overflow-y-auto px-4 py-3">
        {lines.length === 0 && (
          <p className="py-6 text-center text-[12px] leading-relaxed text-fg-muted">
            {inCall
              ? "Say something — I'm listening."
              : "Talk to the assistant. It uses the same tools as the chat, out loud."}
          </p>
        )}
        {lines.map((l, i) => (
          <div
            key={i}
            className={clsx(
              "max-w-[88%] break-words rounded-xl border px-3 py-2 text-[13px] leading-relaxed",
              l.role === "user"
                ? "ml-auto rounded-br-sm border-signal/25 bg-signal/10 text-fg"
                : "rounded-bl-sm border-line bg-ink-800 text-fg-muted",
            )}
          >
            {l.text}
          </div>
        ))}
        {error && (
          <div className="rounded-xl border border-fail/30 bg-fail/10 px-3 py-2 font-mono text-[11px] text-fail">
            {error}
          </div>
        )}
      </div>

      <div className="flex flex-col items-center gap-1.5 border-t border-line py-4">
        <button
          type="button"
          onClick={talk}
          aria-label={inCall ? "Hang up" : "Start talking"}
          className={clsx(
            "grid h-14 w-14 place-items-center rounded-full border transition-all",
            inCall
              ? "border-fail/40 bg-fail/15 text-fail hover:bg-fail/25"
              : "border-signal/40 bg-signal/15 text-signal hover:bg-signal/25 hover:shadow-glow",
            status === "listening" && "animate-pulse",
          )}
        >
          <IconMic className="h-6 w-6" />
        </button>
        <span className="font-mono text-[10px] text-fg-faint">{STATUS_LABEL[status]}</span>
      </div>
    </div>
  );
}
