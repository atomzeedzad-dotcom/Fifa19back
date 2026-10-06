"""Local FIFA19 backend control panel; does not install or patch a game."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tkinter as tk
import urllib.request
from pathlib import Path
from tkinter import messagebox, ttk

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT/'config/server.json'
RUNTIME = Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'AppData/Local')))/'FIFA19LocalFUT'


class Launcher:
    def __init__(self, window):
        self.window = window
        self.process = None
        self.output = None
        self.started = 0
        self.window.title('FIFA19 Local Server')
        self.window.geometry('660x380')
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        frame = ttk.Frame(window, padding=24)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='FIFA19 Local Server', font=('Segoe UI', 21, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='Backend preview • ยังไม่ได้ทดสอบกับตัวเกม FIFA19', font=('Segoe UI', 11)).pack(anchor='w', pady=(8, 18))
        self.status = tk.StringVar(value='หยุดอยู่ — กดเปิด server เพื่อเริ่มใช้งาน')
        ttk.Label(frame, textvariable=self.status, wraplength=590).pack(anchor='w', pady=(0, 18))
        buttons = ttk.Frame(frame)
        buttons.pack(anchor='w')
        self.start_button = ttk.Button(buttons, text='เปิด Server', command=self.start)
        self.start_button.pack(side='left', padx=(0, 10))
        self.stop_button = ttk.Button(buttons, text='หยุด Server', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=(0, 10))
        ttk.Button(buttons, text='ตรวจการเชื่อมต่อ', command=self.check).pack(side='left')
        ttk.Button(frame, text='เปิดโฟลเดอร์เซฟ / Logs', command=self.open_runtime).pack(anchor='w', pady=(20, 8))
        ttk.Label(frame, text='เซฟแยก: %LOCALAPPDATA%\\FIFA19LocalFUT\n'
                  'ชุดนี้ยังต้องทำ routing/TLS ให้ตรงกับไฟล์เกมจริงเมื่อมี FIFA19.exe\n'
                  'พอร์ตและการตั้งค่าอยู่ใน config\\server.json', wraplength=590).pack(anchor='w', pady=8)
        self.window.after(400, self.poll)

    def health(self):
        config = json.loads(CONFIG.read_text(encoding='utf-8-sig'))
        url = f'http://127.0.0.1:{config["ports"]["fut"]}/health'
        with urllib.request.urlopen(url, timeout=0.5) as response:
            document = json.load(response)
        if document.get('product') != 'FIFA19LocalFUT':
            raise ValueError('Configured port belongs to another application')
        return document

    def start(self):
        if self.process:
            return
        try:
            RUNTIME.mkdir(parents=True, exist_ok=True)
            self.output = (RUNTIME/'launcher.log').open('ab')
            executable = Path(sys.executable)
            if executable.name.lower() == 'pythonw.exe':
                executable = executable.with_name('python.exe')
            self.process = subprocess.Popen([str(executable), str(ROOT/'server/localfut19.py'), '--launcher-control'],
                                            cwd=ROOT, stdin=subprocess.PIPE, stdout=self.output, stderr=self.output,
                                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.started = time.monotonic()
            self.status.set('กำลังเปิด server…')
            self.start_button.configure(state='disabled')
            self.stop_button.configure(state='normal')
        except Exception as exc:
            if self.output:
                self.output.close()
                self.output = None
            messagebox.showerror('เปิด Server ไม่สำเร็จ', str(exc))

    def stop(self):
        if self.process and self.process.poll() is None:
            self.status.set('กำลังหยุด server และปิดฐานข้อมูล…')
            self.stop_button.configure(state='disabled')
            try:
                self.process.stdin.write(b'stop\n')
                self.process.stdin.flush()
            except (OSError, ValueError):
                self.process.terminate()

    def poll(self):
        if self.process:
            result = self.process.poll()
            if result is not None:
                self.process.stdin.close()
                self.process = None
                self.output.close()
                self.output = None
                self.start_button.configure(state='normal')
                self.stop_button.configure(state='disabled')
                self.status.set('หยุดอยู่' if result == 0 else 'เปิดไม่สำเร็จ — ตรวจ launcher.log (อาจมีพอร์ตถูกใช้งานอยู่)')
            elif str(self.stop_button['state']) != 'disabled':
                try:
                    document = self.health()
                    self.status.set(f'พร้อมใช้งาน — นักเตะ {document["playerCount"]:,} คน | FUT port {document["ports"]["fut"]}\nการเข้าเกมจริง: ยังไม่ยืนยัน')
                except Exception:
                    if time.monotonic()-self.started > 15:
                        self.status.set('Server ยังไม่ตอบ — ตรวจ launcher.log')
        self.window.after(700, self.poll)

    def check(self):
        try:
            document = self.health()
            self.status.set(f'FUT API เชื่อมต่อได้ — นักเตะ {document["playerCount"]:,} คน; ยังไม่ได้ทดสอบกับเกม')
        except Exception as exc:
            messagebox.showerror('เชื่อมต่อไม่ได้', str(exc))

    def open_runtime(self):
        RUNTIME.mkdir(parents=True, exist_ok=True)
        os.startfile(RUNTIME)

    def close(self):
        if self.process and self.process.poll() is None:
            self.stop()
            self.window.after(200, self.finish_close)
        else:
            self.window.destroy()

    def finish_close(self):
        if self.process and self.process.poll() is None:
            self.window.after(200, self.finish_close)
        else:
            self.window.destroy()


if __name__ == '__main__':
    window = tk.Tk()
    Launcher(window)
    window.mainloop()
