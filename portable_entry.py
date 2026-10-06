"""Single portable executable: GUI by default, owned backend worker on --server."""
from __future__ import annotations

import sys


def main() -> int:
    from app_paths import runtime_root
    runtime = runtime_root()
    runtime.mkdir(parents=True, exist_ok=True)
    # Windowed Windows executables do not have console streams. Logging still
    # needs a real file handle in both the GUI and server process.
    stream = None
    if sys.stdout is None or sys.stderr is None:
        stream = (runtime/'portable.log').open('a', encoding='utf-8', buffering=1)
        sys.stdout = stream
        sys.stderr = stream
    try:
        if '--server' in sys.argv:
            sys.argv.remove('--server')
            from server.localfut19 import main as server_main
            return server_main()
        if '--check-launcher' in sys.argv:
            from tools.check_launcher import run
            return run()
        from Fifaback19Launcher import main as launcher_main
        return launcher_main()
    except Exception:
        import traceback
        traceback.print_exc()
        if '--stop-file' not in sys.argv:
            from tkinter import messagebox
            messagebox.showerror('FIFA19 Local Server', f'Cannot start. Details: {runtime / "portable.log"}')
        return 1
    finally:
        if stream:
            stream.close()


if __name__ == '__main__':
    raise SystemExit(main())
