// Pure helpers behind the Agents drawer (AgentsSidePanel), tested in agentsPanel.test.ts.

export type MergedTool = {
  name: string;
  description: string;
  source: "declared" | "offered" | "called";
  calls: number;
  parameters: Record<string, unknown>;
};
export type MergedAgent = { name: string; description: string; agent_ids: string[]; tools: MergedTool[] };
export type DeclaredAgent = { name: string; description: string; tools: ({ name: string } & Record<string, unknown>)[] } &
  Record<string, unknown>;
export type ObservedAgent = { agent_id: string; system_prompt?: string; models?: string[] } & Record<string, unknown>;
// `agents` is what a judge reads as @AGENTS; `declared` / `observed` are the detail hung off it.
export type AgentsData = { agents: MergedAgent[]; declared: DeclaredAgent[]; observed: ObservedAgent[] };

/** How many tools the traces show that the agent definition left out — 0 when none was declared. */
export function notInCatalog(data: AgentsData): number {
  if (data.declared.length === 0) return 0;
  return data.agents.reduce((n, a) => n + a.tools.filter((t) => t.source !== "declared").length, 0);
}

/** The system prompt and models recovered from an undeclared agent's own spans — only when that
 *  `agent_id` is its alone. Graph frameworks stamp one id on every sub-agent, and the prompt of the
 *  id is then the supervisor's, not this agent's. */
export function observedDetail(agent: MergedAgent, data: AgentsData): ObservedAgent | undefined {
  if (agent.agent_ids.length !== 1) return undefined;
  const [id] = agent.agent_ids;
  if (data.agents.some((a) => a !== agent && a.agent_ids.includes(id))) return undefined;
  return data.observed.find((o) => o.agent_id === id);
}
