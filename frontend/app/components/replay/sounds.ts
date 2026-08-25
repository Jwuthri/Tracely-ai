"use client";

/* Chiptune one-shots for the fleet office, synthesized with Web Audio — no assets, no
   licenses. OFF by default; the 🔊 toggle click is the user gesture that unlocks the
   AudioContext. Every voice is a couple of oscillators through one master gain. */

import { useEffect, useRef } from "react";
import { isContainer, type PlayEvent } from "./timeline";

let ctx: AudioContext | null = null;
let master: GainNode | null = null;

function ensureCtx(): AudioContext | null {
  if (typeof window === "undefined") return null;
  if (!ctx) {
    const AC = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AC) return null;
    ctx = new AC();
    master = ctx.createGain();
    master.gain.value = 0.16; // quiet by design — ambience, not a game over the user's music
    master.connect(ctx.destination);
  }
  void ctx.resume();
  return ctx;
}

/** One decaying square/triangle blip. */
function blip(freq: number, dur = 0.09, type: OscillatorType = "square", vol = 1, when = 0) {
  const c = ensureCtx();
  if (!c || !master) return;
  const t0 = c.currentTime + when;
  const osc = c.createOscillator();
  const g = c.createGain();
  osc.type = type;
  osc.frequency.value = freq;
  g.gain.setValueAtTime(vol, t0);
  g.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
  osc.connect(g).connect(master);
  osc.start(t0);
  osc.stop(t0 + dur + 0.02);
}

const SOUND: Record<string, () => void> = {
  ask: () => { blip(880, 0.08, "triangle"); blip(1320, 0.1, "triangle", 0.8, 0.09); }, // door chime
  llm: () => blip(520, 0.06, "triangle", 0.5),
  tool: () => { blip(300, 0.05, "square", 0.7); blip(420, 0.05, "square", 0.5, 0.06); }, // clack
  skill: () => blip(660, 0.09, "triangle", 0.6), // page flip-ish
  think: () => blip(392, 0.12, "sine", 0.35),
  ring: () => { blip(1180, 0.07, "square", 0.6); blip(1180, 0.07, "square", 0.6, 0.12); }, // ring ring
  error: () => { blip(240, 0.16, "sawtooth", 0.9); blip(160, 0.22, "sawtooth", 0.9, 0.12); }, // buzz down
};

export function verdictJingle(verdict: "PASS" | "FAIL") {
  if (verdict === "PASS") [523, 659, 784, 1047].forEach((f, i) => blip(f, 0.14, "triangle", 0.8, i * 0.11));
  else [392, 370, 349, 311].forEach((f, i) => blip(f, 0.22, "sawtooth", 0.7, i * 0.16)); // sad trombone
}

/** Fire one-shots for beats that STARTED since the last commit. A seek (big jump either way)
 *  plays nothing — scrubbing must not machine-gun the whole script. */
export function useOfficeSounds(events: PlayEvent[], t: number, enabled: boolean) {
  const last = useRef(t);
  useEffect(() => {
    const prev = last.current;
    last.current = t;
    if (!enabled || t <= prev || t - prev > 600) return;
    const started = events.filter((e) => !isContainer(e) && e.pt > prev && e.pt <= t);
    for (const e of started.slice(0, 3)) {
      if (e.status === "error") SOUND.error();
      else if (e.delegate_to) SOUND.ring();
      else SOUND[e.kind]?.();
    }
    // a delegation CONTAINER starting is the phone call itself
    if (events.some((e) => isContainer(e) && e.delegate_to && e.pt > prev && e.pt <= t)) SOUND.ring();
  }, [events, t, enabled]);
}
