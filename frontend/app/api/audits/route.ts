import { NextRequest } from "next/server";
import { forward } from "@/app/lib/forward";

export const GET = (req: NextRequest) => forward(req, "/api/audits");
export const POST = (req: NextRequest) => forward(req, "/api/audits");
