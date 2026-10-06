# Implementation and handoff

The authorized work produced a standalone FIFA19 backend preview in this folder.
The full user objective, entering FUT19 through a local server, remains pending
because the user confirmed there are no game files. Do not present backend tests
as proof that a FIFA19 client connects or gameplay works.

The FIFA18 source implementation and live saves were not modified. Its required
cross-agent review and record-only investigation entry updated its collaboration
records. The source review found a repeated Draft choice can consume another
cached offer. FIFA19 excludes all identified native Draft HTTP dispatch paths,
including normalized numeric squad IDs, rather than claiming the issue repaired.

The engine is a frozen snapshot with exact hashes and bootstrap anchors in
`reference-manifest.json`. Runtime imports the FIFA19 profile before SQLite
initialization, replacing player loading and bronze starter data. The adapter
rewrites the external FIFA19 namespace, session headers and configured endpoints,
and excludes FIFA18 special-card correction tables/catalogues. Source historical
comments and filenames remain FIFA18 so provenance is visible.

Tests use temporary runtime directories and ports. The 14 test methods include
additional subcases for Draft/SBC aliases, leading-zero reserved IDs, JSON IDs,
decoded World Cup query parameters and native POST purchase aliases.
An additional observed two-process smoke run persisted a renamed squad across
interpreter restart and exited cleanly through stdin `stop`. The GUI was tested
with its Tk window withdrawn and an isolated LOCALAPPDATA directory; Start,
Check and Stop worked. All test processes were stopped afterward.

Client adaptation must use the actual FIFA19 build. Current instance string,
FIRE2 schema and service ports are development assumptions. The copied
development TLS certificate is not proven trusted/pinned by FIFA19. Do not reuse
FIFA18 EXE/DLL offsets. The inspector produces a read-only fingerprint/endpoints
report; it does not establish client compatibility or install modifications.
