"use client";

import { DocLink } from "@/app/components/DocLink";
import clsx from "clsx";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  IconArrowLeft,
  IconChat,
  IconClip,
  IconMic,
  IconPlus,
  IconSend,
  IconStack,
  IconTrash,
  IconX,
} from "./icons";
import { VoiceMode } from "./VoiceMode";
import {
  getVoiceCall,
  getVoiceCallServerSnapshot,
  isCallActive,
  subscribeVoiceCall,
} from "@/app/lib/voiceCall";
import { Markdown } from "./Markdown";
import { TimeAgo } from "./TimeAgo";
import { streamAssistantTurn } from "@/app/lib/assistant";
import {
  ALERT_DRAFT_EVENT,
  ASSISTANT_OPEN_EVENT,
  readPageContext,
  type AlertDraftArgs,
} from "@/app/lib/pageContext";
import { ActivityLog, closeActivity, type Activity } from "./ActivityLog";

/* The in-app assistant — a launcher in the bottom-right corner opening a chat panel, mounted
   once in the (app) layout so it survives navigation and knows which page you are on.

   Conversations live in Postgres (`assistant_chats`), not in this browser: the panel has two
   views — the current chat, and the history you can go back into — and coming back tomorrow
   reopens where you left off. All this component keeps locally is WHICH conversation was last
   open. Attachments are uploaded on pick (so the composer can show them, and a slow upload
   doesn't stall the send) and referenced by id from the message.

   A turn streams. The assistant reads traces and writes evaluators to answer, so a question can
   take the better part of a minute — the panel names the tool it is running and types the answer
   out as it arrives, because a minute of three bouncing dots reads as broken. */

const LAST_CHAT = "tracely_chat_last";
const MAX_ATTACHMENTS = 5;
const MAX_FILE_BYTES = 10 * 1024 * 1024;
const ACCEPT =
  "image/*,text/*,.json,.jsonl,.ndjson,.csv,.tsv,.log,.md,.yaml,.yml,.toml,.py,.ts,.tsx,.js,.jsx,.sql,.sh,.xml,.pdf";

export type Attachment = { id: string; name: string; mime: string; size: number };
export type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  attachments?: Attachment[];
  error?: boolean;
};
type ChatSummary = { id: string; title: string; messages: number; updated_at: string | null };

/** Exported for the test: the two pure decisions this widget makes about a file. */
export const isImage = (a: { mime?: string }) => (a.mime ?? "").startsWith("image/");

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** Which of `files` may still be attached, and why the rest may not. */
export function admitFiles(
  files: { name: string; size: number }[],
  alreadyAttached: number,
): { ok: { name: string; size: number }[]; error: string } {
  const room = MAX_ATTACHMENTS - alreadyAttached;
  const tooBig = files.filter((f) => f.size > MAX_FILE_BYTES);
  const fitting = files.filter((f) => f.size <= MAX_FILE_BYTES);
  const ok = fitting.slice(0, Math.max(0, room));
  if (tooBig.length)
    return { ok, error: `${tooBig[0].name} is over ${formatBytes(MAX_FILE_BYTES)}` };
  if (fitting.length > ok.length)
    return { ok, error: `${MAX_ATTACHMENTS} files per message` };
  return { ok, error: "" };
}

function FileChip({ att, onRemove }: { att: Attachment; onRemove?: () => void }) {
  const body = (
    <>
      <IconClip className="h-3 w-3 shrink-0 text-fg-faint" />
      <span className="max-w-[140px] truncate">{att.name}</span>
      <span className="shrink-0 font-mono text-[10px] text-fg-faint">{formatBytes(att.size)}</span>
    </>
  );
  return (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-line bg-ink-800 px-2 py-1 text-[11px] text-fg-muted">
      {onRemove ? (
        body
      ) : (
        <a
          href={`/api/assistant/files/${att.id}`}
          download={att.name}
          className="inline-flex items-center gap-1.5 transition-colors hover:text-fg"
        >
          {body}
        </a>
      )}
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Remove ${att.name}`}
          className="text-fg-faint transition-colors hover:text-fail"
        >
          <IconX className="h-3 w-3" />
        </button>
      )}
    </span>
  );
}

function Attachments({ list }: { list: Attachment[] }) {
  const images = list.filter(isImage);
  const files = list.filter((a) => !isImage(a));
  return (
    <div className="mt-2 space-y-1.5">
      {images.map((a) => (
        // eslint-disable-next-line @next/next/no-img-element -- a proxied blob, not a known-size asset
        <img
          key={a.id}
          src={`/api/assistant/files/${a.id}`}
          alt={a.name}
          className="max-h-40 w-auto rounded-lg border border-line"
        />
      ))}
      {files.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {files.map((a) => (
            <FileChip key={a.id} att={a} />
          ))}
        </div>
      )}
    </div>
  );
}

function Bubble({ message }: { message: ChatMessage }) {
  const mine = message.role === "user";
  // One branch, not stacked utilities: `text-fail` and `text-fg-muted` are both single-class
  // rules, so which one wins is Tailwind's emit order, not the order they appear in here.
  const skin = message.error
    ? "rounded-bl-sm border-fail/30 bg-fail/10 font-mono text-[11px] text-fail"
    : mine
      ? "ml-auto rounded-br-sm border-signal/25 bg-signal/10 text-fg"
      : "rounded-bl-sm border-line bg-ink-800 text-fg-muted";
  return (
    <div
      className={clsx(
        "animate-fadeup max-w-[88%] break-words rounded-xl border px-3 py-2 text-[13px] leading-relaxed",
        skin,
        // a provider's error body can be a paragraph of JSON — let it scroll, not eat the panel
        message.error && "max-h-[150px] overflow-y-auto",
      )}
    >
      {mine || message.error ? (
        <span className="whitespace-pre-wrap">{message.content}</span>
      ) : (
        <Markdown content={message.content} className="assistant-md" />
      )}
      {!!message.attachments?.length && <Attachments list={message.attachments} />}
    </div>
  );
}

function Thinking() {
  return (
    <div className="flex w-fit items-center gap-1 rounded-xl rounded-bl-sm border border-line bg-ink-800 px-3 py-2.5">
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="h-1.5 w-1.5 rounded-full bg-fg-faint"
          style={{ animation: `chat-dot 1.2s ease-in-out ${i * 0.16}s infinite` }}
        />
      ))}
      <style>{`@keyframes chat-dot{0%,100%{opacity:.25;transform:translateY(0)}50%{opacity:1;transform:translateY(-2px)}}
        @media (prefers-reduced-motion: reduce){@keyframes chat-dot{0%,100%{opacity:.6}50%{opacity:1}}}`}</style>
    </div>
  );
}

// Questions that make the agent USE something. The old set ("What is a regression case?") was
// written for a bot that could only explain the product, and quietly taught people it still can.
const SUGGESTIONS = [
  "Why did my last conversation fail?",
  "What's my biggest failure cluster?",
  "Add a column that checks the agent stayed on topic",
];

const ctrl =
  "grid h-6 w-6 place-items-center rounded-md text-fg-faint transition-colors hover:bg-hilite/5 hover:text-fg";

export function Assistant() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<"chat" | "history" | "voice">("chat");
  const [chatId, setChatId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<Attachment[]>([]);
  const [busy, setBusy] = useState(false);
  const [activity, setActivity] = useState<Activity[]>([]); // this turn's tool calls, as they land
  const [uploading, setUploading] = useState(0);
  const [notice, setNotice] = useState("");
  const [noKey, setNoKey] = useState(false);
  const booted = useRef(false);
  // A call runs outside this component (see `voiceCall.ts`), so the launcher can show it is
  // still live after the panel is closed.
  const call = useSyncExternalStore(subscribeVoiceCall, getVoiceCall, getVoiceCallServerSnapshot);
  const onCall = isCallActive(call);
  // The in-flight turn, so it can be cancelled: a tool loop runs for tens of seconds and keeps
  // spending after the user has stopped caring (closing the panel, switching chats, or saying so).
  const inflight = useRef<AbortController | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const filePicker = useRef<HTMLInputElement>(null);

  const loadChats = useCallback(async (): Promise<ChatSummary[]> => {
    const r = await fetch("/api/assistant/chats");
    const list = r.ok ? await r.json().catch(() => []) : [];
    const rows: ChatSummary[] = Array.isArray(list) ? list : [];
    setChats(rows);
    return rows;
  }, []);

  const openChat = useCallback(async (id: string) => {
    const r = await fetch(`/api/assistant/chats/${id}`);
    if (!r.ok) return;
    const data = await r.json().catch(() => null);
    if (!data) return;
    setMessages(data.messages ?? []);
    setChatId(id);
    setView("chat");
    try {
      localStorage.setItem(LAST_CHAT, id);
    } catch {
      // private mode: we just won't remember which conversation was open
    }
  }, []);

  // First open loads the history and reopens where you left off. Deferred until then on
  // purpose — someone who never opens the widget should never pay for its round trips.
  useEffect(() => {
    if (!open || booted.current) return;
    booted.current = true;
    (async () => {
      const rows = await loadChats();
      let last: string | null = null;
      try {
        last = localStorage.getItem(LAST_CHAT);
        localStorage.removeItem("tracely_chat_v1"); // the pre-Postgres transcript
      } catch {
        // no storage: fall through to the newest conversation
      }
      const pick = rows.find((c) => c.id === last) ?? rows[0];
      if (pick) await openChat(pick.id);
    })();
  }, [open, loadChats, openChat]);

  // A page can open the chat with a prompt already typed ("✦ Ask the assistant" on the alert
  // editor) — the user still presses send, so the canvas is never redrawn by a click. It starts
  // a fresh conversation: the boot below would otherwise reopen yesterday's chat underneath the
  // prompt a moment later, and the question would land in the wrong transcript.
  useEffect(() => {
    const onOpen = (e: Event) => {
      const prompt = String((e as CustomEvent<{ prompt?: string }>).detail?.prompt ?? "");
      booted.current = true;
      void loadChats();
      newChat();
      setOpen(true);
      if (prompt) setDraft(prompt);
    };
    window.addEventListener(ASSISTANT_OPEN_EVENT, onOpen);
    return () => window.removeEventListener(ASSISTANT_OPEN_EVENT, onOpen);
  }, []);

  // ⌘J / Ctrl-J opens the assistant, Esc closes it — the same idiom as the ⌘K palette.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() === "j" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setOpen((o) => !o);
      } else if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [messages, busy, view, activity]);
  useEffect(() => {
    if (open && view === "chat") box.current?.focus();
  }, [open, view]);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
  }, [draft]);

  async function attach(files: File[]) {
    const { ok, error } = admitFiles(files, pending.length);
    setNotice(error);
    if (!ok.length) return;
    setUploading((n) => n + ok.length);
    for (const file of ok as File[]) {
      try {
        const form = new FormData();
        form.append("file", file);
        const r = await fetch("/api/assistant/upload", { method: "POST", body: form });
        const data = await r.json().catch(() => null);
        if (r.ok && data?.id) setPending((p) => [...p, data as Attachment]);
        else setNotice(data?.detail ?? `couldn't upload ${file.name}`);
      } catch {
        setNotice(`couldn't upload ${file.name}`);
      } finally {
        setUploading((n) => n - 1);
      }
    }
  }

  async function send(text: string) {
    const question = text.trim();
    if ((!question && !pending.length) || busy) return;
    const attachments = pending;
    setMessages((m) => [...m, { role: "user", content: question, attachments }]);
    setDraft("");
    setPending([]);
    setNotice("");
    setBusy(true);
    setNoKey(false);
    setActivity([]);
    stop(); // a previous turn should never outlive the one replacing it
    const ctl = new AbortController();
    inflight.current = ctl;
    // Whether the answer bubble exists yet: the first delta appends it, every later one grows it.
    // A ref, not state, because the frames arrive faster than a re-render.
    let started = false;
    // `draft_alert`'s arguments ARE the draft. Held until its `tool_done` says the backend
    // accepted them, then handed to whichever page is listening (the alert editor's canvas).
    let alertDraft: AlertDraftArgs | null = null;
    const fail = (content: string) =>
      setMessages((m) => [...m, { role: "assistant", content, error: true }]);
    try {
      await streamAssistantTurn(
        {
          message: question || "(see the attached file)",
          chat_id: chatId,
          attachments,
          path: pathname ?? "",
          context: readPageContext(),
        },
        (e) => {
          if (e.type === "tool") {
            if (e.name === "draft_alert") alertDraft = e.args as AlertDraftArgs;
            return setActivity((a) => [...a, { name: e.name, at: Date.now(), state: "run" }]);
          }
          if (e.type === "tool_done") {
            if (e.name === "draft_alert" && e.ok && alertDraft)
              window.dispatchEvent(new CustomEvent(ALERT_DRAFT_EVENT, { detail: alertDraft }));
            return setActivity((a) => closeActivity(a, e.name, e.ok));
          }
          if (e.type === "delta") {
            setActivity([]);
            const first = !started;
            started = true;
            return setMessages((m) =>
              first
                ? [...m, { role: "assistant", content: e.text }]
                : m.map((msg, i) =>
                    i === m.length - 1 ? { ...msg, content: msg.content + e.text } : msg,
                  ),
            );
          }
          if (e.type === "disabled") return setNoKey(true);
          if (e.type === "over_budget")
            return fail(
              `This conversation has used its $${e.budget_usd.toFixed(2)} assistant budget. ` +
                "Start a new one to keep going.",
            );
          if (e.type === "error") return fail(e.detail || "I couldn't reach the model.");
          if (e.type === "done") {
            // `reply` is authoritative — the deltas are a preview of it, not the record.
            setMessages((m) =>
              started
                ? m.map((msg, i) => (i === m.length - 1 ? { ...msg, content: e.reply } : msg))
                : [...m, { role: "assistant", content: e.reply }],
            );
            setChatId(e.chat_id);
            try {
              localStorage.setItem(LAST_CHAT, e.chat_id);
            } catch {
              // private mode: the conversation is still saved server-side, just not remembered here
            }
            loadChats();
          }
        },
        ctl.signal,
      );
    } catch (err) {
      // An abort is the user's own doing — whatever streamed so far stands, unremarked.
      if (!(err instanceof DOMException && err.name === "AbortError"))
        fail("I couldn't reach the model.");
    } finally {
      if (inflight.current === ctl) inflight.current = null;
      setBusy(false);
      setActivity([]);
    }
  }

  function stop() {
    inflight.current?.abort();
    inflight.current = null;
  }

  // What the voice model's one tool runs: a regular text-assistant turn into the CURRENT
  // conversation, so spoken questions and their answers land in the same saved transcript the
  // chat view shows. Returns the reply for the voice model to speak.
  // A call captures `askTracely` once, at connect time, but `chatId` changes underneath it as
  // the conversation is saved. The ref keeps the running call pointed at the CURRENT turn
  // handler, so a spoken answer never lands in the conversation we were in ten minutes ago.
  const askRef = useRef<(q: string) => Promise<string>>(async () => "");
  const askStable = useCallback((q: string) => askRef.current(q), []);

  const askTracely = useCallback(
    async (question: string): Promise<string> => {
      let reply = "";
      let failed = "";
      await streamAssistantTurn(
        {
          message: question,
          chat_id: chatId,
          attachments: [],
          path: pathname ?? "",
          context: readPageContext(),
        },
        (e) => {
          if (e.type === "error") failed = e.detail || "the assistant failed";
          if (e.type === "over_budget") failed = "this conversation is over its assistant budget";
          if (e.type === "disabled") failed = "no model is configured on this deployment";
          if (e.type === "done") {
            reply = e.reply;
            setChatId(e.chat_id);
            setMessages((m) => [
              ...m,
              { role: "user", content: question },
              { role: "assistant", content: e.reply },
            ]);
            void loadChats();
          }
        },
      );
      if (!reply) throw new Error(failed || "the assistant gave no answer");
      return reply;
    },
    [chatId, pathname, loadChats],
  );
  askRef.current = askTracely;

  // Closing the panel cancels whatever is streaming. There are four ways to close it (button,
  // Escape, launcher, navigation) and a turn kept running costs us money nobody is reading.
  useEffect(() => {
    if (!open) inflight.current?.abort();
  }, [open]);

  function newChat() {
    stop(); // the answer was for the conversation being left behind
    setMessages([]);
    setChatId(null);
    setPending([]);
    setNotice("");
    setView("chat");
    try {
      localStorage.removeItem(LAST_CHAT);
    } catch {
      // nothing to forget
    }
    box.current?.focus();
  }

  async function removeChat(id: string) {
    await fetch(`/api/assistant/chats/${id}`, { method: "DELETE" });
    const rows = await loadChats();
    if (id === chatId) {
      if (rows[0]) await openChat(rows[0].id);
      else newChat();
    }
  }

  const empty = messages.length === 0;
  const canSend = !busy && !uploading && (!!draft.trim() || pending.length > 0);

  return (
    <>
      {open && (
        <div className="animate-fadeup fixed bottom-[84px] right-5 z-40 flex max-h-[min(620px,calc(100vh-130px))] w-[380px] flex-col overflow-hidden rounded-xl border border-line bg-ink-900 shadow-2xl">
          <div className="flex items-center justify-between border-b border-line px-3 py-3">
            <div className="flex min-w-0 items-center gap-2">
              {view === "chat" ? (
                <button
                  type="button"
                  onClick={() => {
                    setView("history");
                    loadChats();
                  }}
                  title="Past conversations"
                  aria-label="Past conversations"
                  className={ctrl}
                >
                  <IconStack className="h-3.5 w-3.5" />
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => setView("chat")}
                  title="Back to the conversation"
                  aria-label="Back to the conversation"
                  className={ctrl}
                >
                  <IconArrowLeft className="h-3.5 w-3.5" />
                </button>
              )}
              <h2 className="truncate text-[13.5px] font-semibold text-fg">
                {view === "history" ? "Conversations" : view === "voice" ? "Speech" : "Assistant"}
              </h2>
              {view === "chat" && <DocLink path="/product/assistant" />}
            </div>
            <div className="flex items-center gap-1.5">
              {view === "chat" && (
                <button
                  type="button"
                  onClick={() => setView("voice")}
                  title="Speech mode"
                  aria-label="Speech mode"
                  className={ctrl}
                >
                  <IconMic className="h-3.5 w-3.5" />
                </button>
              )}
              {view === "chat" && !empty && (
                <button
                  type="button"
                  onClick={newChat}
                  title="New conversation"
                  aria-label="New conversation"
                  className={ctrl}
                >
                  <IconPlus className="h-3.5 w-3.5" />
                </button>
              )}
              <span className="h-3.5 w-px bg-line" aria-hidden />
              <button
                type="button"
                onClick={() => setOpen(false)}
                title="Close"
                aria-label="Close assistant"
                className={ctrl}
              >
                <IconX className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>

          {view === "voice" ? (
            <VoiceMode askTracely={askStable} />
          ) : view === "history" ? (
            <div className="flex-1 overflow-y-auto py-1">
              {chats.length === 0 ? (
                <p className="px-4 py-8 text-center text-[12px] text-fg-muted">
                  No conversations yet.
                </p>
              ) : (
                <ul>
                  {chats.map((c) => (
                    <li key={c.id} className="group flex items-center gap-1 px-2">
                      <button
                        type="button"
                        onClick={() => openChat(c.id)}
                        className={clsx(
                          "min-w-0 flex-1 rounded-md px-2 py-2 text-left transition-colors hover:bg-hilite/5",
                          c.id === chatId && "bg-signal/10",
                        )}
                      >
                        <span className="block truncate text-[13px] text-fg">{c.title}</span>
                        <span className="mt-0.5 block font-mono text-[10px] text-fg-faint">
                          {c.messages} messages · <TimeAgo ts={c.updated_at} />
                        </span>
                      </button>
                      <button
                        type="button"
                        onClick={() => removeChat(c.id)}
                        title={`Delete "${c.title}"`}
                        aria-label={`Delete conversation: ${c.title}`}
                        className="grid h-6 w-6 shrink-0 place-items-center rounded-md text-fg-faint opacity-0 transition-all hover:bg-fail/10 hover:text-fail focus:opacity-100 group-hover:opacity-100"
                      >
                        <IconTrash className="h-3.5 w-3.5" />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ) : (
            <>
              <div
                ref={scroller}
                className="flex-1 space-y-3 overflow-y-auto overflow-x-hidden px-4 py-3"
              >
                {empty ? (
                  <div className="py-6 text-center">
                    <p className="text-[13px] text-fg">Ask me about Tracely.</p>
                    <p className="mx-auto mt-1 max-w-[260px] text-[12px] leading-relaxed text-fg-muted">
                      Traces, evaluators, failure clusters, regression cases, CI gates — how the
                      loop fits together and what to do next. Drop in a screenshot or a log and
                      I&apos;ll read it.
                    </p>
                    <div className="mt-4 flex flex-col items-center gap-1.5">
                      {SUGGESTIONS.map((s) => (
                        <button key={s} type="button" onClick={() => send(s)} className="btn-ghost">
                          {s}
                        </button>
                      ))}
                    </div>
                  </div>
                ) : (
                  messages.map((m, i) => <Bubble key={i} message={m} />)
                )}
                {busy && (
                  <div className="flex flex-col gap-1.5">
                    {activity.length ? <ActivityLog items={activity} /> : <Thinking />}
                  </div>
                )}
                {noKey && (
                  <div className="animate-fadeup rounded-xl border border-warn/30 bg-warn/10 px-3 py-2 text-[12px] leading-relaxed text-warn">
                    The assistant has no model configured on this deployment — set
                    <code className="mx-1 font-mono text-[11px]">OPENROUTER_API_KEY</code>
                    on the backend and ask again.
                  </div>
                )}
              </div>

              <div
                className="border-t border-line p-2.5"
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => {
                  e.preventDefault();
                  attach([...e.dataTransfer.files]);
                }}
              >
                {(pending.length > 0 || uploading > 0 || notice) && (
                  <div className="mb-2 flex flex-wrap items-center gap-1.5">
                    {pending.map((a) => (
                      <FileChip
                        key={a.id}
                        att={a}
                        onRemove={() => setPending((p) => p.filter((x) => x.id !== a.id))}
                      />
                    ))}
                    {uploading > 0 && (
                      <span className="font-mono text-[10px] text-fg-faint">
                        uploading {uploading}…
                      </span>
                    )}
                    {notice && <span className="font-mono text-[10px] text-fail">{notice}</span>}
                  </div>
                )}
                <div className="flex items-end gap-1.5">
                  <input
                    ref={filePicker}
                    type="file"
                    multiple
                    accept={ACCEPT}
                    className="hidden"
                    onChange={(e) => {
                      attach([...(e.target.files ?? [])]);
                      e.target.value = ""; // so picking the same file twice still fires
                    }}
                  />
                  <button
                    type="button"
                    onClick={() => filePicker.current?.click()}
                    title="Attach a file or image"
                    aria-label="Attach a file or image"
                    className="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-fg-faint transition-colors hover:bg-hilite/5 hover:text-fg"
                  >
                    <IconClip className="h-4 w-4" />
                  </button>
                  <textarea
                    ref={box}
                    rows={1}
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    onPaste={(e) => {
                      const files = [...e.clipboardData.files];
                      if (files.length) {
                        e.preventDefault();
                        attach(files);
                      }
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        send(draft);
                      }
                    }}
                    placeholder="Ask anything…"
                    aria-label="Message the assistant"
                    className="max-h-[120px] flex-1 resize-none bg-transparent px-1 py-1.5 text-[13px] leading-relaxed text-fg placeholder:text-fg-faint focus:outline-none"
                  />
                  {busy ? (
                    <button
                      type="button"
                      onClick={stop}
                      aria-label="Stop"
                      title="Stop"
                      className="grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-line bg-ink-800 text-fg-muted transition-colors hover:border-fail/40 hover:text-fail"
                    >
                      <span className="h-2.5 w-2.5 rounded-[2px] bg-current" />
                    </button>
                  ) : (
                    <button
                      type="button"
                      onClick={() => send(draft)}
                      disabled={!canSend}
                      aria-label="Send"
                      className="grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-signal/40 bg-signal/15 text-signal transition-all hover:bg-signal/25 hover:shadow-glow disabled:opacity-40 disabled:hover:shadow-none"
                    >
                      <IconSend className="h-4 w-4" />
                    </button>
                  )}
                </div>
              </div>
            </>
          )}
        </div>
      )}

      <button
        type="button"
        onClick={() => {
          // Mid-call the launcher is the way BACK to the call, not a toggle that hides it.
          if (onCall && !open) setView("voice");
          setOpen((o) => !o);
        }}
        aria-label={
          onCall
            ? "Voice call in progress — open the assistant"
            : open
              ? "Close assistant"
              : "Open assistant (⌘J)"
        }
        title={onCall ? "Voice call in progress — ⌘J" : "Assistant — ⌘J"}
        className={clsx(
          "group fixed bottom-5 right-5 z-40 grid h-[52px] w-[52px] place-items-center rounded-full border bg-ink-800/90 shadow-lg backdrop-blur-md transition-transform hover:scale-105",
          onCall ? "border-signal/60 text-signal" : "border-line text-signal",
        )}
      >
        {/* A call keeps running with the panel shut, so the launcher has to say so — otherwise
            the only evidence you are still on a live mic is the browser's own tab indicator. */}
        {onCall && (
          <span
            aria-hidden
            className="absolute inset-0 animate-ping rounded-full border border-signal/40"
          />
        )}
        {open ? (
          <IconX className="h-5 w-5" />
        ) : onCall ? (
          <IconMic className="h-5 w-5" />
        ) : (
          <IconChat className="h-5 w-5" />
        )}
      </button>
    </>
  );
}
