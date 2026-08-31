import { describe, it, expect } from "vitest";
import type { OsdsCommand } from "@osds/adapter-kit";
import {
  handleListingUpsert,
  withSubject,
  type Listing,
  type ListingUpsertResult,
} from "./listing-upsert.js";

function command(
  payload: Record<string, unknown>,
  over: Partial<OsdsCommand> = {},
): OsdsCommand {
  return {
    command: "listing.upsert",
    idempotency_key: "csv:row-42",
    tenant_id: "tnt_chicago",
    adapter_id: "webhook",
    trace_id: "01JBQ7X2M4K8ZP3RVN6T9WGYHD",
    payload,
    ...over,
  };
}

const stored: Listing = {
  id: "listing_01JBQ6YW8TFN2H5CKXQ4V3ZDAE",
  tenant_id: "tnt_chicago",
  slug: "hoffman-plumbing-lakeview",
  name: "Hoffman Plumbing",
  description: "Emergency plumbing in Lakeview.",
  categories: ["plumbers", "emergency-plumbers"],
  location: {
    address_line1: "1422 W Belmont Ave",
    address_line2: null,
    locality: "Chicago",
    region: "IL",
    postal_code: "60657",
    country: "US",
    lat: 41.9395,
    lon: -87.664,
    geo_precision: "rooftop",
  },
  contact: {
    phone_e164: "+17735550142",
    email: "office@hoffmanplumbing.example",
    website: "https://hoffmanplumbing.example",
  },
};

/** Narrow to an event-bearing outcome, failing loudly on `rejected` / `unchanged`. */
function expectEvent(
  r: ListingUpsertResult,
): Extract<ListingUpsertResult, { outcome: "created" | "updated" }> {
  if (r.outcome === "rejected" || r.outcome === "unchanged") {
    throw new Error(
      `expected an event, got "${r.outcome}": ${JSON.stringify(r)}`,
    );
  }
  return r;
}

describe("listing.upsert - create", () => {
  it("no current listing -> a listing.created draft with no subject", () => {
    const res = handleListingUpsert(
      command({
        slug: "acme-plumbing",
        name: "  Acme Plumbing  ",
        categories: ["plumbers", "plumbers"],
        contact: { email: "HELLO@ACME.EXAMPLE", phone_e164: "+13125550188" },
      }),
      null,
    );

    const ok = expectEvent(res);
    expect(ok.outcome).toBe("created");
    expect(ok.match).toEqual({
      by: "slug",
      tenant_id: "tnt_chicago",
      slug: "acme-plumbing",
    });

    if (ok.outcome !== "created") throw new Error("unreachable");
    expect(ok.event.type).toBe("listing.created");
    // The draft carries no subject at all - not `null`, absent.
    expect("subject" in ok.event).toBe(false);
    expect(ok.event.data.listing).toEqual({
      id: null,
      tenant_id: "tnt_chicago",
      slug: "acme-plumbing",
      name: "Acme Plumbing",
      description: null,
      categories: ["plumbers"],
      location: {
        address_line1: null,
        address_line2: null,
        locality: null,
        region: null,
        postal_code: null,
        country: null,
        lat: null,
        lon: null,
        geo_precision: "none",
      },
      contact: {
        phone_e164: "+13125550188",
        email: "hello@acme.example",
        website: null,
      },
    });
  });

  it("withSubject turns a create draft into an emittable event", () => {
    const res = handleListingUpsert(
      command({ slug: "acme-plumbing", name: "Acme" }),
      null,
    );
    const ok = expectEvent(res);
    if (ok.outcome !== "created") throw new Error("unreachable");

    const event = withSubject(ok.event, "listing_minted_1");

    expect(event).toEqual({
      type: "listing.created",
      subject: "listing_minted_1",
      data: {
        listing: {
          id: "listing_minted_1",
          tenant_id: "tnt_chicago",
          slug: "acme-plumbing",
          name: "Acme",
          description: null,
          categories: [],
          location: {
            address_line1: null,
            address_line2: null,
            locality: null,
            region: null,
            postal_code: null,
            country: null,
            lat: null,
            lon: null,
            geo_precision: "none",
          },
          contact: { phone_e164: null, email: null, website: null },
        },
      },
    });
    // The draft is not mutated.
    expect("subject" in ok.event).toBe(false);
    expect(ok.event.data.listing.id).toBeNull();
  });

  it("matches on id when the payload carries one", () => {
    const res = handleListingUpsert(
      command({ id: "listing_seed_1", slug: "acme-plumbing", name: "Acme" }),
      null,
    );
    const ok = expectEvent(res);
    expect(ok.match).toEqual({ by: "id", id: "listing_seed_1" });
  });
});

describe("listing.upsert - update", () => {
  it("existing listing -> listing.updated with an RFC 6902 JSON Patch of the changes", () => {
    const res = handleListingUpsert(
      command({
        slug: "hoffman-plumbing-lakeview",
        name: "Hoffman Plumbing & Heating",
        categories: ["plumbers", "hvac"],
        location: { address_line2: "Suite 200", geo_precision: "street" },
      }),
      stored,
    );

    const ok = expectEvent(res);
    expect(ok.outcome).toBe("updated");
    expect(ok.match).toEqual({
      by: "slug",
      tenant_id: "tnt_chicago",
      slug: "hoffman-plumbing-lakeview",
    });

    if (ok.outcome !== "updated") throw new Error("unreachable");
    expect(ok.event.type).toBe("listing.updated");
    expect(ok.event.subject).toBe(stored.id);
    expect(ok.event.data.changes).toEqual([
      { op: "replace", path: "/categories", value: ["plumbers", "hvac"] },
      { op: "replace", path: "/location/address_line2", value: "Suite 200" },
      { op: "replace", path: "/location/geo_precision", value: "street" },
      { op: "replace", path: "/name", value: "Hoffman Plumbing & Heating" },
    ]);
  });

  it("clears a field when the payload sets it to null", () => {
    const res = handleListingUpsert(
      command({ description: null, contact: { website: null } }),
      stored,
    );
    const ok = expectEvent(res);
    if (ok.outcome !== "updated") throw new Error("unreachable");
    expect(ok.event.data.changes).toEqual([
      { op: "replace", path: "/contact/website", value: null },
      { op: "replace", path: "/description", value: null },
    ]);
  });

  it("matches on id when the payload carries one, ignoring the slug", () => {
    const res = handleListingUpsert(
      command({ id: stored.id, name: "Renamed" }),
      stored,
    );
    const ok = expectEvent(res);
    expect(ok.match).toEqual({ by: "id", id: stored.id });
    expect(ok.outcome).toBe("updated");
  });
});

describe("listing.upsert - unchanged", () => {
  it("returns `unchanged` with no event when the patch would be empty", () => {
    const res = handleListingUpsert(
      command({ slug: stored.slug, name: stored.name }),
      stored,
    );
    expect(res.outcome).toBe("unchanged");
    expect(res).toEqual({
      outcome: "unchanged",
      match: { by: "slug", tenant_id: "tnt_chicago", slug: stored.slug },
    });
    expect("event" in res).toBe(false);
  });

  it("a payload that only re-cases the email is not a change", () => {
    const res = handleListingUpsert(
      command({ contact: { email: "OFFICE@HOFFMANPLUMBING.EXAMPLE" } }),
      stored,
    );
    expect(res.outcome).toBe("unchanged");
  });
});

describe("listing.upsert - email is lowercased", () => {
  it("lands lowercased in the created listing", () => {
    const res = handleListingUpsert(
      command({
        slug: "acme",
        name: "Acme",
        contact: { email: "Sales@Acme.Example" },
      }),
      null,
    );
    const ok = expectEvent(res);
    if (ok.outcome !== "created") throw new Error("unreachable");
    expect(ok.event.data.listing.contact.email).toBe("sales@acme.example");
  });

  it("is lowercased before it is compared and before it lands in the patch", () => {
    const res = handleListingUpsert(
      command({ contact: { email: "NEW.OFFICE@Hoffmanplumbing.Example" } }),
      stored,
    );
    const ok = expectEvent(res);
    if (ok.outcome !== "updated") throw new Error("unreachable");
    expect(ok.event.data.changes).toEqual([
      {
        op: "replace",
        path: "/contact/email",
        value: "new.office@hoffmanplumbing.example",
      },
    ]);
  });
});

describe("listing.upsert - rejections", () => {
  it("rejects a payload carrying tier", () => {
    const res = handleListingUpsert(
      command({ slug: "acme", name: "Acme", tier: "featured" }),
      null,
    );
    expect(res.outcome).toBe("rejected");
    if (res.outcome !== "rejected") throw new Error("unreachable");
    expect(res.problem.status).toBe(422);
    expect(res.problem.errors).toEqual([
      "payload.tier is not accepted - tier is derived from entitlement (§6)",
    ]);
  });

  it("rejects a payload carrying status", () => {
    const res = handleListingUpsert(
      command({ slug: "acme", name: "Acme", status: "claimed" }),
      null,
    );
    expect(res.outcome).toBe("rejected");
    if (res.outcome !== "rejected") throw new Error("unreachable");
    expect(res.problem.status).toBe(422);
    expect(res.problem.errors).toEqual([
      "payload.status is not accepted - status moves through the claim flow (§9)",
    ]);
  });

  it("rejects tier even when its value is null", () => {
    const res = handleListingUpsert(
      command({ slug: "acme", name: "Acme", tier: null }),
      null,
    );
    expect(res.outcome).toBe("rejected");
  });

  it("rejects a create that is missing required fields", () => {
    const res = handleListingUpsert(
      command({ description: "no name or slug" }),
      null,
    );
    expect(res.outcome).toBe("rejected");
    if (res.outcome !== "rejected") throw new Error("unreachable");
    expect(res.problem.status).toBe(422);
    expect(res.problem.errors).toEqual([
      "payload.slug is required when the listing does not exist",
      "payload.name is required when the listing does not exist",
    ]);
  });

  it("rejects a malformed envelope", () => {
    const res = handleListingUpsert(
      command({ slug: "acme", name: "Acme" }, { tenant_id: "", trace_id: "" }),
      null,
    );
    expect(res.outcome).toBe("rejected");
    if (res.outcome !== "rejected") throw new Error("unreachable");
    expect(res.problem.detail).toBe("malformed command envelope");
  });

  it("rejects an unknown field inside location", () => {
    const res = handleListingUpsert(
      command({ slug: "acme", name: "Acme", location: { city: "Chicago" } }),
      null,
    );
    expect(res.outcome).toBe("rejected");
    if (res.outcome !== "rejected") throw new Error("unreachable");
    expect(JSON.stringify(res.problem.errors)).toContain("unknown field");
  });

  it("rejects a non-E.164 phone number", () => {
    const res = handleListingUpsert(
      command({
        slug: "acme",
        name: "Acme",
        contact: { phone_e164: "773-555-0142" },
      }),
      null,
    );
    expect(res.outcome).toBe("rejected");
  });
});

describe("listing.upsert - tenant scoping", () => {
  it("allows the same slug to be created in two different tenants", () => {
    const a = handleListingUpsert(
      command(
        { slug: "acme-plumbing", name: "Acme A" },
        { tenant_id: "tnt_a" },
      ),
      null,
    );
    const b = handleListingUpsert(
      command(
        { slug: "acme-plumbing", name: "Acme B" },
        { tenant_id: "tnt_b" },
      ),
      null,
    );

    const okA = expectEvent(a);
    const okB = expectEvent(b);

    expect(okA.outcome).toBe("created");
    expect(okB.outcome).toBe("created");
    expect(okA.match).toEqual({
      by: "slug",
      tenant_id: "tnt_a",
      slug: "acme-plumbing",
    });
    expect(okB.match).toEqual({
      by: "slug",
      tenant_id: "tnt_b",
      slug: "acme-plumbing",
    });
    if (okA.outcome !== "created" || okB.outcome !== "created")
      throw new Error("unreachable");
    expect(okA.event.data.listing.tenant_id).toBe("tnt_a");
    expect(okB.event.data.listing.tenant_id).toBe("tnt_b");
  });
});
