import { describe, expect, it } from "vitest";
import { layoutOffice, poseAt } from "@/app/components/replay/office";
import { OFFICE_PACING, toPlayEvents } from "@/app/components/replay/timeline";
import { ACTORS, SCENE } from "./FleetPeek";

/* The landing-page office is hand-written data run through the product's pose engine, so the
   thing that can rot is the DATA: a typo'd actor id or station string leaves the marketing
   page showing three people who never move. Assert the scene actually plays. */

const { events, total } = toPlayEvents(SCENE, OFFICE_PACING);
const layout = layoutOffice(ACTORS);
const pose = (id: string, t: number) =>
  poseAt(ACTORS.find((a) => a.id === id)!, events, t, layout, 0);

describe("landing fleet scene", () => {
  it("seats every actor", () => {
    for (const a of ACTORS) expect(layout.desks[a.id]).toBeDefined();
  });

  it("sends the support agent to the library, then phones the sub-agent", () => {
    const skill = events.find((e) => e.kind === "skill")!;
    expect(pose("support", skill.pt + 100).at).toBe("library");
    const handoff = events.find((e) => e.delegate_to)!;
    const p = pose("support", handoff.pt + 100);
    expect(p.at).toBe("desk"); // the handoff is a phone call, not a walk
    expect(p.bubble).toMatchObject({ type: "speech", text: "☎ pull order #8412", faded: false });
    expect(pose("orders", handoff.pt + 100).at).toBe("desk"); // the callee mans their own desk
  });

  it("runs the sub-agent's tool at the tool wall", () => {
    const tool = events.find((e) => e.actor === "orders" && e.kind === "tool")!;
    expect(pose("orders", tool.pt + 100).at).toBe("tools");
  });

  it("raises the failure as an error bubble", () => {
    const boom = events.find((e) => e.status === "error")!;
    expect(pose("billing", boom.pt + 100).bubble).toEqual({ type: "error", text: "check_eligibility", faded: false });
  });

  it("ends on the reply that will be graded", () => {
    const reply = events[events.length - 1];
    // the reply is readable WHILE it is being written — an in-flight llm used to be a bare "…"
    expect(pose("support", reply.pt + reply.pdur - 50).bubble).toMatchObject({ type: "speech", text: "Sure — your refund is on its way!", faded: false });
    expect(pose("support", total).bubble).toEqual({ type: "speech", text: "Sure — your refund is on its way!", faded: false });
  });
});
