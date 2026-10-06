"""Graphical launcher for the portable FIFA19 local backend preview."""
from __future__ import annotations
import json, os, queue, subprocess, sys, threading, time, tkinter as tk, urllib.request, uuid
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from app_paths import VERSION, app_root, runtime_root

ROOT = app_root()
CONFIG = ROOT/'config/server.json'
RUNTIME = runtime_root()

class Launcher:
    def __init__(self, window):
        self.window = window
        self.process = None
        self.output = None
        self.started = 0
        self.stop_started = None
        self.stop_file = None
        self.last_start_error = None
        self.closing = False
        self.report_queue = queue.Queue()
        self.report_busy = False
        self.last_report = None
        self.window.title(f'FIFA19 Local Server v{VERSION}')
        self.window.geometry('780x590')
        self.window.minsize(720, 570)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style(window)
        style.theme_use('clam')
        style.configure('TFrame', background='#f1f4f9')
        style.configure('TLabel', background='#f1f4f9', foreground='#172238', font=('Segoe UI', 11))
        style.configure('TButton', font=('Segoe UI', 11), padding=(14, 10))
        style.configure('Accent.TButton', background='#2459cf', foreground='white')
        style.map('Accent.TButton', background=[('active', '#1746ab'), ('disabled', '#9bacca')])
        frame = ttk.Frame(window, padding=26)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='FIFA19 Local Server', font=('Segoe UI', 25, 'bold')).pack(anchor='w')
        ttk.Label(frame, text=f'Windows Portable  •  v{VERSION}', foreground='#516078').pack(anchor='w', pady=(4, 16))
        ttk.Label(frame, text='ตัวทดลอง server — ยังเข้าเล่น FUT19 ผ่าน launcher นี้ไม่ได้', foreground='#92520d', font=('Segoe UI', 12, 'bold')).pack(anchor='w', pady=(0, 14))
        self.status = tk.StringVar(value='พร้อมเปิด local server โดยไม่ต้องพิมพ์คำสั่ง')
        ttk.Label(frame, textvariable=self.status, wraplength=710).pack(anchor='w', pady=(0, 15))
        buttons = ttk.Frame(frame)
        buttons.pack(anchor='w')
        self.start_button = ttk.Button(buttons, text='เปิด Local Server', command=self.start, style='Accent.TButton')
        self.start_button.pack(side='left', padx=(0, 10))
        self.stop_button = ttk.Button(buttons, text='หยุด Server', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=(0, 10))
        ttk.Button(buttons, text='ตรวจทุกบริการ', command=self.check).pack(side='left')
        ttk.Separator(frame).pack(fill='x', pady=18)
        self.game_path = tk.StringVar(value=self.load_game_path())
        ttk.Label(frame, text='ตำแหน่งเกม FIFA19 (เตรียมไว้สำหรับตรวจการเชื่อมต่อภายหลัง)', font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        game_row = ttk.Frame(frame)
        game_row.pack(fill='x', pady=(8, 6))
        ttk.Entry(game_row, textvariable=self.game_path, state='readonly').pack(side='left', fill='x', expand=True)
        ttk.Button(game_row, text='เลือก FIFA19.exe', command=self.select_game).pack(side='left', padx=(10, 0))
        ttk.Label(frame, text='โปรแกรมนี้ไม่มีไฟล์เกม ต้องมี FIFA19 ที่ติดตั้งแยกต่างหาก\nการเชื่อมต่อเกมและโหมด FUT19 ยังอยู่ระหว่างพัฒนา', foreground='#516078').pack(anchor='w', pady=6)
        options = ttk.Frame(frame)
        options.pack(anchor='w', pady=(12, 10))
        ttk.Button(options, text='เปิดเซฟ / Logs', command=self.open_runtime).pack(side='left', padx=(0, 10))
        ttk.Button(options, text='คู่มือ', command=self.open_help).pack(side='left', padx=(0, 10))
        ttk.Button(options, text='รายงานตัวเกม', command=self.inspect_game).pack(side='left')
        ttk.Label(frame, text='เซฟเก็บในเครื่องของแต่ละคน • server เปิดเฉพาะเครื่องนี้\nรุ่น Portable รวม Python และส่วนประกอบไว้แล้ว', foreground='#516078', font=('Segoe UI', 10)).pack(anchor='w', pady=8)
        self.window.after(400, self.poll)

    def load_game_path(self):
        try:
            return json.loads((RUNTIME/'launcher-settings.json').read_text(encoding='utf-8')).get('gamePath', '')
        except (OSError, ValueError):
            return ''

    def select_game(self):
        selected = filedialog.askopenfilename(title='เลือก FIFA19.exe', filetypes=[('FIFA19', 'FIFA19.exe')])
        if not selected:
            return
        if Path(selected).name.lower() != 'fifa19.exe':
            messagebox.showerror('เลือกไฟล์เกม', 'กรุณาเลือกไฟล์ FIFA19.exe')
            return
        self.game_path.set(selected)
        RUNTIME.mkdir(parents=True, exist_ok=True)
        (RUNTIME/'launcher-settings.json').write_text(json.dumps({'gamePath': selected}), encoding='utf-8')

    def inspect_game(self):
        selected = self.game_path.get()
        if not selected:
            messagebox.showinfo('รายงานตัวเกม', 'เลือก FIFA19.exe ก่อน โปรแกรมจะอ่านข้อมูลและสร้างรายงาน')
            return
        def action():
            from tools.inspect_game import inspect
            return inspect(Path(selected))
        self.run_report('game', action)

    def run_report(self, kind, action):
        if self.report_busy or self.closing:
            return
        self.report_busy = True
        self.last_report = None
        self.status.set('กำลังตรวจไฟล์เกม…' if kind == 'game' else 'กำลังตรวจ Redirector / Blaze / EASW / FUT…')
        def work():
            try:
                self.report_queue.put((kind, action(), None))
            except Exception as exc:
                self.report_queue.put((kind, None, str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def collect_report(self):
        try:
            kind, report, error = self.report_queue.get_nowait()
        except queue.Empty:
            return
        self.report_busy = False
        if error:
            self.status.set('ตรวจไม่สำเร็จ: '+error)
            if not self.closing:
                messagebox.showerror('รายงาน', error)
            return
        try:
            RUNTIME.mkdir(parents=True, exist_ok=True)
            destination = RUNTIME/('fifa19-client-report.json' if kind == 'game' else 'connection-report.json')
            destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as exc:
            self.status.set('บันทึกรายงานไม่สำเร็จ: '+str(exc))
            return
        self.last_report = report
        if kind == 'game':
            self.status.set(f'ตรวจไฟล์เกมแล้ว ({report["pe"]["architecture"]}) — ยังไม่มีแพตช์ FIFA19 ที่ยืนยันแล้ว')
            if not self.closing:
                messagebox.showinfo('รายงานตัวเกม', f'อ่าน EXE และ DLL ที่เกี่ยวข้องแล้ว\nบันทึกรายงาน: {destination}\nยังไม่ได้แก้ไฟล์เกมหรือยืนยันการเข้าเล่น FUT19')
        else:
            failed = [row['service'] for row in report['checks'] if not row['passed']]
            self.status.set(('ตรวจผ่านทั้ง 5 บริการ — ยังไม่ยืนยันการเข้าเกมจริง' if not failed else 'บริการที่ตรวจไม่ผ่าน: '+', '.join(failed))+'\nดูรายละเอียดใน connection-report.json')

    def health(self):
        config = json.loads(CONFIG.read_text(encoding='utf-8-sig'))
        url = f'http://127.0.0.1:{config["ports"]["fut"]}/health'
        with urllib.request.urlopen(url, timeout=0.35) as response:
            document = json.load(response)
        if document.get('product') != 'FIFA19LocalFUT':
            raise ValueError('Configured port belongs to another application')
        if self.process and document.get('processId') != self.process.pid:
            raise ValueError('A different server already owns this port')
        return document

    def start(self):
        if self.process:
            return
        self.last_start_error = None
        self.last_report = None
        try:
            RUNTIME.mkdir(parents=True, exist_ok=True)
            control = RUNTIME/'control'
            control.mkdir(parents=True, exist_ok=True)
            self.stop_file = control/f'{uuid.uuid4().hex}.stop'
            self.output = (RUNTIME/'launcher.log').open('ab')
            if getattr(sys, 'frozen', False):
                command = [sys.executable, '--server', '--stop-file', str(self.stop_file)]
            else:
                executable = Path(sys.executable)
                if executable.name.lower() == 'pythonw.exe':
                    executable = executable.with_name('python.exe')
                command = [str(executable), str(ROOT/'server/localfut19.py'), '--stop-file', str(self.stop_file)]
            environment = os.environ.copy()
            environment['FIFA19_LOCAL_RUNTIME'] = str(RUNTIME)
            self.process = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL, stdout=self.output, stderr=self.output, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.started = time.monotonic()
            self.stop_started = None
            self.status.set('กำลังเปิด server… ครั้งแรกจะเตรียมเซฟและ TLS ในเครื่องนี้')
            self.start_button.configure(state='disabled')
            self.stop_button.configure(state='normal')
        except Exception as exc:
            self.last_start_error = str(exc)
            self.cleanup()
            messagebox.showerror('เปิด Server ไม่สำเร็จ', str(exc))

    def cleanup(self):
        self.process = None
        if self.output:
            self.output.close()
            self.output = None
        if self.stop_file:
            self.stop_file.unlink(missing_ok=True)
            self.stop_file = None
        self.start_button.configure(state='normal')
        self.stop_button.configure(state='disabled')

    def stop(self):
        if self.process and self.process.poll() is None and self.stop_started is None:
            self.stop_started = time.monotonic()
            self.status.set('กำลังหยุด server และปิดฐานข้อมูล…')
            self.stop_button.configure(state='disabled')
            self.stop_file.touch()

    def poll(self):
        self.collect_report()
        if self.process:
            result = self.process.poll()
            if result is not None:
                if result != 0:
                    self.last_start_error = 'Server exited with code '+str(result)
                self.cleanup()
                self.status.set('หยุดอยู่ — เปิดใหม่ได้ทุกเมื่อ' if result == 0 else 'เปิดไม่สำเร็จ — ดู launcher.log (อาจมีพอร์ตถูกใช้งานอยู่)')
            elif self.stop_started is not None:
                if time.monotonic()-self.stop_started > 10:
                    self.process.terminate()
            elif not self.report_busy and self.last_report is None:
                try:
                    document = self.health()
                    self.status.set(f'Local server ทำงานแล้ว • นักเตะ {document["playerCount"]:,} คน\nการเชื่อมต่อและเล่น FUT19 จริงยังไม่พร้อม')
                except Exception:
                    if time.monotonic()-self.started > 15:
                        self.status.set('Server ยังไม่ตอบ — เปิด Logs เพื่อตรวจรายละเอียด')
        if self.closing and self.process is None:
            self.window.destroy()
            return
        self.window.after(500, self.poll)

    def check(self):
        expected_pid = self.process.pid if self.process else None
        def action():
            from server.diagnostics import diagnose
            config = json.loads(CONFIG.read_text(encoding='utf-8-sig'))
            return diagnose(config, RUNTIME, expected_pid)
        self.run_report('connection', action)

    def open_runtime(self):
        RUNTIME.mkdir(parents=True, exist_ok=True)
        os.startfile(RUNTIME)

    def open_help(self):
        for candidate in ('START_HERE.txt', 'README.md'):
            path = ROOT/candidate
            if path.is_file():
                os.startfile(path)
                return
        messagebox.showinfo('คู่มือ', 'เปิด Local Server แล้วกดตรวจการเชื่อมต่อ รุ่นนี้ยังเข้า FUT19 จริงไม่ได้')

    def close(self):
        self.closing = True
        if self.process and self.process.poll() is None:
            self.stop()
        else:
            self.cleanup()
            self.window.destroy()

def main() -> int:
    window = tk.Tk()
    Launcher(window)
    window.mainloop()
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
