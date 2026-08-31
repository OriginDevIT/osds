import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { parseNear } from "./lib/near";

/**
 * `/search?near=` accepts coordinates only (`lat,lon`). OSDS ships no geocoder,
 * so a postal code or place name is a 400 - not a silent empty result set.
 * A page (Server Component) cannot set an arbitrary status, so this runs here.
 */
export function middleware(request: NextRequest): NextResponse {
  const near = request.nextUrl.searchParams.get("near");
  if (parseNear(near).kind === "invalid") {
    return new NextResponse(
      "The 'near' parameter must be coordinates as lat,lon " +
        "(for example near=41.94,-87.64). OSDS ships no geocoder, so postal " +
        "codes and place names are not accepted.\n",
      { status: 400, headers: { "content-type": "text/plain; charset=utf-8" } },
    );
  }
  return NextResponse.next();
}

export const config = {
  matcher: "/search",
};
