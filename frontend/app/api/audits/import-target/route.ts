import { NextRequest } from "next/server";
import { forward } from "@/app/lib/forward";

// Rebuild a target (system prompt, tools, model) from one of the workspace's agents.
export const POST = (req: NextRequest) => forward(req, "/api/audits/import-target");
