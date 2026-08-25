import { ConversationHeader, ConversationTabs, EvalsPill } from "@/app/components/ConversationChrome";
import { OfficeStage } from "@/app/components/replay/OfficeStage";
import { loadConversation } from "@/app/lib/conversation";

export const metadata = { title: "Fleet · Tracely" };

export default async function FleetPage({ params }: { params: Promise<{ threadId: string }> }) {
  const { threadId } = await params;
  const thread = decodeURIComponent(threadId);
  const { turns, usage, agentRef, spans, verdict } = await loadConversation(thread);
  return (
    <div className="space-y-6">
      <ConversationHeader threadId={thread} turns={turns.length} usage={usage}
        agentRef={agentRef} firstInput={turns[0]?.input ?? ""} />
      <ConversationTabs threadId={thread} active="fleet" spans={spans}
        right={<EvalsPill threadId={thread} verdict={verdict} />} />
      <OfficeStage threadId={thread} verdict={verdict} />
    </div>
  );
}
