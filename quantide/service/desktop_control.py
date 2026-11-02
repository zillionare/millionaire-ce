"""Small foreground controller, independent of the web service's lifetime."""

import ctypes
import os
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox

from quantide.config.paths import get_app_config_dir


def _controller_buttons(window, action):
    """Build service actions and schedule controller status refreshes."""
    controls = tk.Frame(window)
    controls.pack(pady=8)
    for title, command in (("启动", "start"), ("停止", "stop"), ("状态", "status")):
        tk.Button(controls, text=title, command=lambda name=command: action(name)).pack(
            side="left", padx=5
        )
    tk.Button(
        window, text="打开网页界面", command=lambda: webbrowser.open("http://127.0.0.1:8130")
    ).pack(pady=5)
    tk.Button(
        window,
        text="日志位置",
        command=lambda: messagebox.showinfo(
            "日志",
            str(get_app_config_dir() / "logs")
            if os.name != "nt"
            else os.path.join(
                os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "MillionaireCE", "logs"
            ),
        ),
    ).pack()


def create_controller():
    """Build the native controls without transferring service process ownership."""
    window = tk.Tk()
    window.title("Millionaire CE")
    window.geometry("440x240")
    status = tk.StringVar(value="Millionaire CE 后台服务")
    action_lock = threading.Lock()
    tk.Label(window, textvariable=status, wraplength=410).pack(pady=18)

    def action(command):
        def work():
            with action_lock:
                try:
                    if sys.platform == "win32" and not ctypes.windll.shell32.IsUserAnAdmin():
                        arguments = f"-m quantide.cli service {command}"
                        result = ctypes.windll.shell32.ShellExecuteW(
                            None, "runas", sys.executable, arguments, None, 1
                        )
                        message = "已请求管理员操作" if result > 32 else "管理员操作未执行"
                    else:
                        result = subprocess.run(
                            [sys.executable, "-m", "quantide.cli", "service", command],
                            capture_output=True,
                            text=True,
                            timeout=40,
                        )
                        message = (result.stdout or result.stderr or f"exit={result.returncode}")[
                            -500:
                        ]
                    window.after(0, status.set, message)
                except Exception as exc:
                    window.after(0, status.set, str(exc))

        threading.Thread(target=work, daemon=True).start()

    _controller_buttons(window, action)
    return window


def main():
    """Open service controls; closing this window leaves the background service running."""
    create_controller().mainloop()
    return 0
