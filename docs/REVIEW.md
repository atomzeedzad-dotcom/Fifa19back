# Independent reference review

Verdict: **approved for the stated backend/API preview scope** (2026-10-06, Codex-Reference-Reviewer). This does not approve actual FIFA19 game connectivity or gameplay.

Scope: adapter/profile, roster import, frozen-snapshot tooling, launcher, integration tests and documentation, followed by targeted re-review of the amended exclusion gates.

## Findings resolved

- Native `/sbs` SBC routes and Draft choices/choose aliases, generic award/match routes, reserved squad IDs, decoded WC query values and nested JSON mode/squad selectors are rejected before inherited dispatch.
- Numeric reserved Draft squad paths now use integer interpretation: `/squad/0900001` and `/squad/000900002` return 501. Body/query reserved IDs, including leading-zero strings and numeric JSON values, are checked too.
- Native POST/PUT `/purchased/items` and `/purchased` aliases are rejected; GET item lists remain available. The store catalog is empty, avoiding unsupported FIFA18 event packs, special odds and rarity guarantees.
- Player definitions retain provisional common rarity. `/sqbt` and `/squadbattle` are rejected and corresponding settings are disabled.

No remaining material issue was found in the amended exclusion gates reviewed. The final additions also reject inherited SquadBattle metadata aliases `/featuredsquad`, `squad-battle` and `squad/battle`; independent HTTP tests observed 501 for all three.

## Observed verification

`python -B -m unittest tests.test_localfut19 -v`: **14 tests passed in 1.843 seconds** after the final amendments. Tests used a temporary `FIFA19_LOCAL_RUNTIME` and ephemeral loopback ports and shut down their services. Boundary checks cover the original review reproductions, leading-zero Draft squad paths, reserved numeric JSON values, native purchase aliases and SquadBattle endpoints. Previous independent isolated probes established the defects before they were fixed. After the last three metadata-alias additions, an independent run of `python -B -m unittest tests.test_localfut19.ServerIntegrationTests.test_unsupported_features_and_old_routes_are_explicit` passed in 0.940 seconds, including all three 501 responses.

Snapshot hashes pass. No native game patch or executable/DLL offsets were copied. The FIFA19 roster is installed before database initialization; starter cards use FIFA19 ratings/year, distinct identities and 23 populated slots. Default runtime/database and ports are separate from FIFA18. Representative Blaze/FUT configuration, TLS framing, offline engine access, persistent squad writes and same-process service restart are tested.

## Limitations

No FIFA19 game files are available. Actual routing, TLS trust/pinning, authentication/entitlements, wire compatibility, gameplay, cosmetics and native rendering remain unverified. Development TLS reuse proves neither game trust nor game connectivity. The roster is a historical partial snapshot with unverified rarity and some league IDs. Inherited cosmetics, consumables, market/season metadata and remaining wire behavior have not been established against a FIFA19 client.

The launcher GUI was inspected statically. Service restart tests reuse the Python process; they do not prove clean-process migration or complete GUI lifecycle behavior. The engine is process-global, so runtime selection must precede its first import. Approval applies only to the documented preview and inspected fixes, not to all inherited engine behavior or the unresolved FIFA18 source Draft implementation.

No installed game, hosts, certificate store, FIFA18 records or live saves were accessed or changed. Only this review report was written.

