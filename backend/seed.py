"""FlowGate seed data: the thirteen contract resources."""

from app.store import upsert_resource

ROADMAP_CONTENT = (
    "Project Nova confidential roadmap. The Project Nova launch date is March 15 "
    "with a total budget of 4.2 million dollars. Phase one delivers the core platform "
    "milestone including authentication billing and audit logging. Phase two ships the "
    "partner integration rollout with staged regional availability and dedicated support "
    "staffing. Phase three completes advanced analytics migration plus performance "
    "hardening before general availability. Access is restricted to Project Nova "
    "leadership and clearance holders only. Tracking identifier CANARY-ALPHA-NOVA-7F3K9 "
    "is embedded for provenance verification. Any external disclosure of this roadmap "
    "requires written approval from the program steering committee."
)

LEAK_SUMMARY = (
    "Project Nova private roadmap. The Project Nova launch date is March 15 "
    "with an overall budget of 4.2 million dollars. Phase one delivers the core platform "
    "milestone including authentication billing and audit logging. Phase two releases the "
    "partner integration rollout with phased regional availability and dedicated support "
    "staffing. Phase three finishes advanced analytics migration plus performance "
    "hardening before general availability. Access is limited to Project Nova "
    "leadership and clearance holders only. Tracking identifier CANARY-ALPHA-NOVA-7F3K9 "
    "is embedded for provenance verification. Any external disclosure of this summary "
    "needs written permission from the program steering committee."
)

ALPHA_NOTES_CONTENT = (
    "Alpha lab maintenance notice. Standup moved to Thursday morning in room 3B. "
    "Bring laptops and badges. Contact Priya for door entry codes."
)

BETA_NOTES_CONTENT = (
    "Beta gardening club update. Tomatoes peppers and basil sprouted near the greenhouse "
    "benches. Saturday volunteers water seedlings sweep paths and repaint fences. "
    "Newcomers collect gloves from the shed locker."
)

PUBLIC_SOURCE_CONTENT = (
    "Springvale Community Open Day takes place on Saturday June 14 from ten in the "
    "morning until four in the afternoon at the Riverside Community Centre. Families "
    "can enjoy craft stalls, cooking demonstrations, live music, guided garden tours, "
    "and children's games on the lawn. Volunteers will hand out printed programmes at "
    "the entrance, and the cafe will serve lunch snacks and drinks throughout the day. "
    "Everyone is welcome, entry is free, and no booking is needed."
)

PUBLIC_DRAFT_CONTENT = (
    "The Riverside centre opens its doors on Saturday 14 June, 10am to 4pm, for a free "
    "family festival. Visitors can browse handmade goods, watch food demonstrations, "
    "hear local performers, explore the gardens with guides, and join outdoor games "
    "for youngsters. Pick up a schedule at the door and stop by the cafe for "
    "refreshments - all are invited and reservations are not required."
)

ALPHA_DRAFT_CONTENT = (
    "Nova is pencilled in to go live in mid-March, backed by about four million "
    "in funding. Its introduction will be staged, starting with the main system "
    "and adding partner links and reporting later. Treat this as restricted; do "
    "not share beyond senior staff."
)


def seed_data(conn) -> None:
    """Upsert exactly the thirteen contract resources."""
    upsert_resource(conn, "alpha-roadmap", "doc", "alpha", 2, ROADMAP_CONTENT)
    upsert_resource(conn, "alpha-notes", "doc", "alpha", 1, ALPHA_NOTES_CONTENT)
    upsert_resource(conn, "beta-notes", "doc", "beta", 1, BETA_NOTES_CONTENT)
    upsert_resource(conn, "shared-board", "kv", "shared", 0, "")
    upsert_resource(conn, "shared-scratch", "kv", "shared", 0, "")
    upsert_resource(conn, "alpha-draft", "doc", "alpha", 2, "")
    upsert_resource(conn, "public-source", "doc", "shared", 0, PUBLIC_SOURCE_CONTENT)
    upsert_resource(conn, "public-draft", "doc", "shared", 0, "")
    upsert_resource(conn, "public-outbox", "kv", "shared", 0, "")
    upsert_resource(conn, "challenge-outbox", "kv", "shared", 0, "")
    upsert_resource(conn, "tenant-alpha-brief", "doc", "alpha", 2, "")
    upsert_resource(conn, "tenant-beta-brief", "doc", "beta", 2, "")
    upsert_resource(conn, "published-releases", "doc", "shared", 0, "")
