"""Exercise the real bundled GUI lifecycle with its window withdrawn."""
from __future__ import annotations

import json
import struct
import sys
import tempfile
import time
from pathlib import Path


def run() -> int:
    import Fifaback19Launcher as app
    window = app.tk.Tk()
    window.withdraw()
    launcher = app.Launcher(window)
    failures = []
    app.messagebox.showerror = lambda title, message: failures.append(title+': '+message)
    app.messagebox.showinfo = lambda *_: None

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
        spin(lambda: not launcher.report_busy and launcher.last_report is not None)
        if launcher.last_report['status'] != 'passed' or len(launcher.last_report['checks']) != 5:
            raise RuntimeError('GUI five-service check failed: '+json.dumps(launcher.last_report))
        if failures:
            raise RuntimeError('; '.join(failures))
        report['checks'].append('GUI Check verified Redirector TLS, Blaze Ping/PreAuth, EASW and both FUT ports')
        with tempfile.TemporaryDirectory(prefix='fut19-pe-fixture-') as temporary:
            # A static PE header fixture exercises bundled scanner dependencies.
            # It is never executed or represented as a FIFA19 game build.
            data = bytearray(512)
            data[:2] = b'MZ'
            struct.pack_into('<I', data, 60, 128)
            data[128:132] = b'PE\0\0'
            struct.pack_into('<HH', data, 132, 0x8664, 1)
            struct.pack_into('<H', data, 148, 240)
            struct.pack_into('<H', data, 152, 0x20b)
            data += b'http://example.gosredirector.ea.com/\0'
            executable = Path(temporary)/'FIFA19.exe'
            executable.write_bytes(data)
            launcher.game_path.set(str(executable))
            launcher.inspect_game()
            spin(lambda: not launcher.report_busy and launcher.last_report is not None)
            inspected = launcher.last_report
            if inspected['pe']['architecture'] != 'x64' or inspected['clientVerified'] or not inspected['readOnly']:
                raise RuntimeError('GUI inspector did not provide correct static evidence')
            if executable.read_bytes() != data:
                raise RuntimeError('Read-only inspector altered its PE fixture')
            launcher.game_path.set('')
        report['checks'].append('GUI read-only EXE report works on a synthetic PE fixture; real FIFA19 remains unverified')
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
