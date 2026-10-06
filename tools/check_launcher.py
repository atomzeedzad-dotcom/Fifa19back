"""Exercise the real bundled GUI lifecycle with its window withdrawn."""
from __future__ import annotations

import json
import sys
import time


def run() -> int:
    import Fifaback19Launcher as app
    window = app.tk.Tk()
    window.withdraw()
    launcher = app.Launcher(window)
    failures = []
    app.messagebox.showerror = lambda title, message: failures.append(title+': '+message)

    def spin(predicate, seconds=20):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            window.update()
            if predicate():
                return
            if failures:
                raise RuntimeError('; '.join(failures))
            time.sleep(0.05)
        raise RuntimeError('Launcher lifecycle check timed out: '+launcher.status.get())

    def ready():
        if launcher.last_start_error:
            raise RuntimeError(launcher.last_start_error)
        try:
            return launcher.health().get('playerCount', 0) > 15000
        except (OSError, ValueError):
            return False

    report = {'status': 'failed', 'frozen': bool(getattr(sys, 'frozen', False)), 'checks': []}
    try:
        launcher.start()
        spin(ready)
        report['checks'].append('GUI Start launched and verified its owned server')
        launcher.check()
        if failures:
            raise RuntimeError('; '.join(failures))
        report['checks'].append('GUI Check received a valid FUT19 health response')
        launcher.stop()
        spin(lambda: launcher.process is None)
        if launcher.last_start_error:
            raise RuntimeError(launcher.last_start_error)
        report['checks'].append('GUI Stop shut down its worker gracefully')
        launcher.start()
        spin(ready)
        report['checks'].append('GUI restart succeeds')
        launcher.close()
        # Run the Tk mainloop so the registered close/stop callbacks complete.
        window.mainloop()
        if launcher.last_start_error:
            raise RuntimeError(launcher.last_start_error)
        report['checks'].append('Closing the GUI stops its active worker')
        report['status'] = 'passed'
    except Exception as exc:
        report['error'] = str(exc)
        if launcher.process and launcher.process.poll() is None:
            launcher.process.kill()
            launcher.process.wait(timeout=5)
        launcher.cleanup()
        try:
            window.destroy()
        except app.tk.TclError:
            pass
    app.RUNTIME.mkdir(parents=True, exist_ok=True)
    (app.RUNTIME/'launcher-check.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 0 if report['status'] == 'passed' else 1
