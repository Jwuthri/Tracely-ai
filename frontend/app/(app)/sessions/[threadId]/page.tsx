import { AwaitingTurns } from "@/app/components/AwaitingTurns";
import { ConversationHeader } from "@/app/components/ConversationChrome";
import { SessionView } from "@/app/components/SessionView";
import { loadConversation } from "@/app/lib/conversation";

export default async function ThreadPage({ params, searchParams }: {
  params: Promise<{ threadId: string }>;
  searchParams: Promise<{ view?: string }>;
}) {
  const { threadId } = await params;
  const { view } = await searchParams;
  // Next hands dynamic params percent-encoded; every link below encodes once more.
  const thread = decodeURIComponent(threadId);
  const { conv, turns, usage, agentRef } = await loadConversation(thread);

  return (
    <div className="space-y-6">
      <ConversationHeader threadId={thread} turns={turns.length} usage={usage}
        agentRef={agentRef} firstInput={turns[0]?.input ?? ""} />

      {turns.length === 0 ? (
        // Not "not found" — a scenario run opens this page before its first turn is driven.
        <AwaitingTurns threadId={thread} />
      ) : (
        <div className="reveal" style={{ animationDelay: "60ms" }}>
          <SessionView conv={conv} turns={turns} views
            initialTab={view === "timeline" ? "timeline" : "table"} />
        </div>
      )}
    </div>
  );
}
