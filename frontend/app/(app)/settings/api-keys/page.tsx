import { DocLink } from "@/app/components/DocLink";
import { CopyId } from "@/app/components/CopyId";
import { LlmKeyPanel } from "@/app/components/LlmKeyPanel";
import { AssistantModelPanel } from "@/app/components/AssistantModelPanel";
import { getMe } from "@/app/lib/auth";

function mask(k: string) {
  return k.length > 12 ? `${k.slice(0, 6)}…${k.slice(-4)}` : k;
}

export default async function ApiKeysPage() {
  const me = await getMe();
  const keys = me?.ingest_keys ?? [];
  const sample = keys[0] ?? "<your-ingest-key>";

  return (
    <div className="space-y-7">
      <header className="reveal">
        <div className="flex items-center gap-3"><h1 className="font-display text-[24px] font-extrabold tracking-tight">API keys</h1><DocLink path="/product/settings#api-keys" /></div>
        <p className="mt-1.5 text-[14px] text-fg-muted">
          The keys this workspace sends traces with, and the one it runs AI features on. Treat
          both like passwords.
        </p>
      </header>

      <section className="card overflow-hidden">
        <div className="hairline px-4 py-3 text-[13px] font-semibold text-fg">Ingest keys</div>
        {keys.length === 0 ? (
          <div className="px-4 py-8 text-center text-[13px] text-fg-faint">No ingest keys yet.</div>
        ) : (
          keys.map((k) => (
            <div
              key={k}
              className="flex items-center justify-between border-b border-line/50 px-4 py-3 last:border-0"
            >
              <code className="font-mono text-[12.5px] text-fg">{mask(k)}</code>
              <CopyId value={k} text="copy" label="ingest key" />
            </div>
          ))
        )}
      </section>

      <LlmKeyPanel />
      <AssistantModelPanel />

      <section className="card p-5">
        <div className="mb-2 text-[13px] font-semibold text-fg">Send your first trace</div>
        <pre className="overflow-x-auto rounded-lg border border-line bg-ink-900 p-4 font-mono text-[12px] leading-relaxed text-fg-muted">
{`import tracely_sdk as tracely

tracely.init(
    endpoint="${process.env.NEXT_PUBLIC_TRACELY_PUBLIC_API ?? "https://your-tracely-host"}",
    api_key="${sample}",
)`}
        </pre>
      </section>
    </div>
  );
}
