// The assistant's speech mode: one entry point (`startVoiceSession`) over two transports.
// OpenAI is WebRTC — the browser exchanges SDP with api.openai.com using an ephemeral token and
// the session config (instructions, tools, VAD) was already baked in server-side when the token
// was minted. Grok (xAI) is a raw WebSocket — same ephemeral-token idea, but audio is PCM16
// base64 in JSON frames and the client sends the `session.update` the backend handed it.
//
// Either way the voice model has exactly one tool, `ask_tracely`, and this module forwards it
// to the caller's `askTracely` (the regular text assistant over /api/assistant) and returns the
// answer — so speech grants no reach the chat widget didn't already have.

export type VoiceProviderInfo = {
  id: "openai" | "xai";
  label: string;
  model: string;
  voices: string[];
  default_voice: string;
};

export type VoiceStatus = "connecting" | "listening" | "speaking" | "thinking" | "closed";

export type VoiceEvent =
  | { type: "status"; status: VoiceStatus }
  | { type: "user_transcript"; text: string }
  | { type: "assistant_transcript"; text: string; final: boolean }
  | { type: "tool"; question: string }
  | { type: "tool_done" }
  | { type: "error"; detail: string };

export type VoiceHandle = { stop: () => void };

type SessionInfo = {
  provider: "openai" | "xai";
  token: string;
  voice: string;
  connection: {
    type: "webrtc" | "websocket";
    url: string;
    session_update?: Record<string, unknown>;
  };
};

/** Convert one Float32 capture buffer to base64 PCM16 (what the realtime APIs eat). */
export function float32ToPcm16Base64(f32: Float32Array): string {
  const pcm = new Int16Array(f32.length);
  for (let i = 0; i < f32.length; i++) {
    const s = Math.max(-1, Math.min(1, f32[i]));
    pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  const bytes = new Uint8Array(pcm.buffer);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}

/** Convert a base64 PCM16 audio delta back to Float32 for Web Audio playback. */
export function pcm16Base64ToFloat32(b64: string): Float32Array {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const pcm = new Int16Array(bytes.buffer, 0, bytes.length >> 1);
  const f32 = new Float32Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / (pcm[i] < 0 ? 0x8000 : 0x7fff);
  return f32;
}

export async function fetchVoiceProviders(): Promise<VoiceProviderInfo[]> {
  const r = await fetch("/api/assistant/voice/config", { cache: "no-store" });
  if (!r.ok) return [];
  const data = await r.json().catch(() => null);
  return Array.isArray(data?.providers) ? data.providers : [];
}

export async function startVoiceSession(opts: {
  provider: "openai" | "xai";
  voice: string;
  askTracely: (question: string) => Promise<string>;
  onEvent: (e: VoiceEvent) => void;
}): Promise<VoiceHandle> {
  const r = await fetch("/api/assistant/voice/session", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ provider: opts.provider, voice: opts.voice }),
  });
  const info = (await r.json().catch(() => null)) as SessionInfo | { detail?: string } | null;
  if (!r.ok || !info || !("token" in info))
    throw new Error((info as { detail?: string })?.detail ?? "couldn't start a voice session");
  return info.connection.type === "webrtc"
    ? startWebRtc(info, opts)
    : startWebSocket(info, opts);
}

type Opts = Parameters<typeof startVoiceSession>[0];

/** Run one ask_tracely call and answer the model back on `send`. Shared by both transports —
 *  the wire shapes (function_call_output item + response.create) are identical. */
async function runTool(
  args: string,
  callId: string,
  opts: Opts,
  send: (obj: Record<string, unknown>) => void,
) {
  let question = "";
  try {
    question = String(JSON.parse(args || "{}")?.question ?? "");
  } catch {
    /* the model produced malformed args; ask it to retry via the error output below */
  }
  opts.onEvent({ type: "tool", question });
  opts.onEvent({ type: "status", status: "thinking" });
  let output: string;
  try {
    output = question ? await opts.askTracely(question) : "error: no question was provided";
  } catch (err) {
    output = `error: ${err instanceof Error ? err.message : "the Tracely agent was unreachable"}`;
  }
  opts.onEvent({ type: "tool_done" });
  send({
    type: "conversation.item.create",
    item: { type: "function_call_output", call_id: callId, output: output.slice(0, 8000) },
  });
  send({ type: "response.create" });
}

/** Route one realtime server event (both providers speak the same OpenAI event family). Returns
 *  true if the event was consumed. Audio deltas are NOT handled here — they differ per transport. */
function routeEvent(
  m: { type?: string; transcript?: string; delta?: string; arguments?: string; call_id?: string },
  opts: Opts,
  send: (obj: Record<string, unknown>) => void,
): void {
  const t = m.type ?? "";
  if (t === "conversation.item.input_audio_transcription.completed" && m.transcript) {
    opts.onEvent({ type: "user_transcript", text: m.transcript });
  } else if (t.endsWith("audio_transcript.delta") && m.delta) {
    opts.onEvent({ type: "assistant_transcript", text: m.delta, final: false });
  } else if (t.endsWith("audio_transcript.done")) {
    opts.onEvent({ type: "assistant_transcript", text: "", final: true });
  } else if (t === "response.function_call_arguments.done") {
    void runTool(m.arguments ?? "", m.call_id ?? "", opts, send);
  } else if (t === "input_audio_buffer.speech_started") {
    opts.onEvent({ type: "status", status: "listening" });
  } else if (t === "output_audio_buffer.started" || t === "response.created") {
    opts.onEvent({ type: "status", status: "speaking" });
  } else if (t === "response.done" || t === "output_audio_buffer.stopped") {
    opts.onEvent({ type: "status", status: "listening" });
  } else if (t === "error") {
    const detail = (m as { error?: { message?: string } }).error?.message ?? "voice session error";
    opts.onEvent({ type: "error", detail });
  }
}

// ── OpenAI: WebRTC ────────────────────────────────────────────────────────────

async function startWebRtc(info: SessionInfo, opts: Opts): Promise<VoiceHandle> {
  opts.onEvent({ type: "status", status: "connecting" });
  const pc = new RTCPeerConnection();
  const audio = document.createElement("audio");
  audio.autoplay = true;
  pc.ontrack = (e) => (audio.srcObject = e.streams[0]);
  let mic: MediaStream;
  try {
    mic = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    pc.close(); // a denied mic must not leak the connection
    throw err;
  }
  pc.addTrack(mic.getTracks()[0]);

  const dc = pc.createDataChannel("oai-events");
  const send = (obj: Record<string, unknown>) => {
    if (dc.readyState === "open") dc.send(JSON.stringify(obj));
  };
  dc.onmessage = (e) => {
    try {
      routeEvent(JSON.parse(e.data), opts, send);
    } catch {
      /* non-JSON frame: ignore */
    }
  };
  dc.onopen = () => opts.onEvent({ type: "status", status: "listening" });

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  const sdp = await fetch(info.connection.url, {
    method: "POST",
    body: offer.sdp,
    headers: { Authorization: `Bearer ${info.token}`, "Content-Type": "application/sdp" },
  });
  if (!sdp.ok) {
    mic.getTracks().forEach((t) => t.stop());
    pc.close();
    throw new Error("the voice connection was refused");
  }
  await pc.setRemoteDescription({ type: "answer", sdp: await sdp.text() });

  return {
    stop() {
      mic.getTracks().forEach((t) => t.stop());
      dc.close();
      pc.close();
      audio.srcObject = null;
      opts.onEvent({ type: "status", status: "closed" });
    },
  };
}

// ── xAI (Grok): WebSocket + Web Audio ────────────────────────────────────────

// One inline AudioWorklet that hands raw capture frames back to the main thread. A worklet, not
// the deprecated ScriptProcessor, because Chrome logs a warning per session on the latter.
const TAP_WORKLET = `registerProcessor("tracely-pcm-tap", class extends AudioWorkletProcessor {
  process(inputs) { const c = inputs[0] && inputs[0][0]; if (c) this.port.postMessage(c.slice(0)); return true; }
});`;

async function startWebSocket(info: SessionInfo, opts: Opts): Promise<VoiceHandle> {
  opts.onEvent({ type: "status", status: "connecting" });
  // Ask for 24kHz to match the session config; if the hardware refuses, the session.update
  // below is patched with whatever rate the context actually runs at.
  let ctx: AudioContext;
  try {
    ctx = new AudioContext({ sampleRate: 24000 });
  } catch {
    ctx = new AudioContext();
  }
  const rate = ctx.sampleRate;
  let mic: MediaStream;
  try {
    mic = await navigator.mediaDevices.getUserMedia({ audio: true });
    await ctx.audioWorklet.addModule(
      URL.createObjectURL(new Blob([TAP_WORKLET], { type: "application/javascript" })),
    );
  } catch (err) {
    void ctx.close(); // browsers cap live AudioContexts — a denied mic must not leak one
    throw err;
  }
  const source = ctx.createMediaStreamSource(mic);
  const tap = new AudioWorkletNode(ctx, "tracely-pcm-tap");
  source.connect(tap);

  // Playback: schedule each PCM delta right after the previous one; barge-in resets the clock.
  let playHead = 0;
  const playing: AudioBufferSourceNode[] = [];
  const playDelta = (b64: string) => {
    const f32 = pcm16Base64ToFloat32(b64);
    if (!f32.length) return;
    const buf = ctx.createBuffer(1, f32.length, rate);
    buf.getChannelData(0).set(f32);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    playHead = Math.max(playHead, ctx.currentTime) ;
    src.start(playHead);
    playHead += buf.duration;
    playing.push(src);
    src.onended = () => playing.splice(playing.indexOf(src), 1);
  };
  const stopPlayback = () => {
    playing.splice(0).forEach((s) => {
      try {
        s.stop();
      } catch {
        /* already ended */
      }
    });
    playHead = 0;
  };

  // The cookbook's documented browser handshake: the ephemeral token rides a subprotocol.
  const ws = new WebSocket(info.connection.url, [
    "realtime",
    `openai-insecure-api-key.${info.token}`,
    "openai-beta.realtime-v1",
  ]);
  const send = (obj: Record<string, unknown>) => {
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  };

  let configured = false;
  // Batch mic frames to ~100ms per append instead of one message per 128-sample render quantum.
  let chunk: number[] = [];
  tap.port.onmessage = (e: MessageEvent<Float32Array>) => {
    if (!configured) return;
    chunk.push(...e.data);
    if (chunk.length >= rate / 10) {
      send({ type: "input_audio_buffer.append", audio: float32ToPcm16Base64(new Float32Array(chunk)) });
      chunk = [];
    }
  };

  ws.onmessage = (e) => {
    let m: { type?: string; delta?: string };
    try {
      m = JSON.parse(e.data);
    } catch {
      return;
    }
    if (m.type === "conversation.created" && !configured) {
      const session = structuredClone(info.connection.session_update ?? {}) as {
        audio?: { input?: { format?: { rate?: number } }; output?: { format?: { rate?: number } } };
      };
      if (session.audio?.input?.format) session.audio.input.format.rate = rate;
      if (session.audio?.output?.format) session.audio.output.format.rate = rate;
      send({ type: "session.update", session });
      return;
    }
    if (m.type === "session.updated" && !configured) {
      configured = true;
      opts.onEvent({ type: "status", status: "listening" });
      return;
    }
    if (m.type === "response.output_audio.delta" && m.delta) return playDelta(m.delta);
    if (m.type === "input_audio_buffer.speech_started") stopPlayback(); // barge-in
    routeEvent(m, opts, send);
  };
  ws.onerror = () => opts.onEvent({ type: "error", detail: "voice connection failed" });
  ws.onclose = () => opts.onEvent({ type: "status", status: "closed" });

  return {
    stop() {
      configured = false;
      stopPlayback();
      tap.port.onmessage = null;
      mic.getTracks().forEach((t) => t.stop());
      ws.close();
      void ctx.close();
      opts.onEvent({ type: "status", status: "closed" });
    },
  };
}
