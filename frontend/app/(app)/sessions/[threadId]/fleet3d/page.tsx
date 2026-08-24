import { ConversationHeader, ConversationTabs, EvalsPill } from "@/app/components/ConversationChrome";
import { OfficeStage3D } from "@/app/components/replay/OfficeStage3D";
import { loadConversation } from "@/app/lib/conversation";

export const metadata = { title: "Fleet 3D · Tracely" };

export default async function Fleet3DPage({ params }: { params: Promise<{ threadId: string }> }) {
  const { threadId } = await params;
  const thread = decodeURIComponent(threadId);
  const { turns, usage, agentRef, spans, verdict } = await loadConversation(thread);
  return (
    <div className="space-y-6">
      <ConversationHeader threadId={thread} turns={turns.length} usage={usage}
        agentRef={agentRef} firstInput={turns[0]?.input ?? ""} />
      <ConversationTabs threadId={thread} active="fleet3d" spans={spans}
        right={<EvalsPill threadId={thread} verdict={verdict} />} />
      <OfficeStage3D threadId={thread} />
    </div>
  );
}
