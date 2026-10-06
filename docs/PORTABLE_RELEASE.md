# Portable launcher v0.2.0

The portable ZIP includes the Python runtime, Tk GUI, cryptography, frozen
backend modules, the FIFA19 base roster, and default configuration. Users
extract the ZIP and open FIFA19LocalServer.exe; Python/pip/PowerShell are not
part of the user flow. Data/config/_internal must remain beside the executable.

FIFA19LocalServer.exe runs the GUI by default and launches its owned backend
worker with --server. Worker shutdown uses a unique local stop file because
windowed executables do not provide normal console stdin. The GUI waits for
graceful shutdown and terminates only its own worker after a ten-second timeout.
Health responses contain a PID so the GUI cannot mistake an existing server
for its newly launched worker. Saves, logs, game-folder preferences and fresh
TLS identities live under the user's local runtime directory.

Cryptography creates a per-machine localhost certificate/private key on first
startup. The legacy FIFA18 private key is not shipped. This certificate is for
backend development and does not establish FIFA19 client TLS trust/pinning.

The game-file picker and report button provide read-only executable inspection.
There is no functional Play FUT19 action: FIFA19 client routing/session/wire
integration remains unfinished without actual game files. v0.2.0 is explicitly
published as a prerelease backend preview, not a playable game release.

Verification must cover the actual extracted EXE with Python removed from PATH,
including Unicode/spaces in paths, TLS/Blaze/FUT traffic, SQLite persistence
across EXE processes and GUI Start/Check/Stop/Restart/Close. Only a passed
verification report matching the ZIP checksum can be published by the release
tool. No game/host/certificate-store installation is performed.

PyInstaller's portable runtime/file layout follows its official documentation:
https://pyinstaller.org/en/stable/runtime-information.html
https://pyinstaller.org/en/stable/spec-files.html
