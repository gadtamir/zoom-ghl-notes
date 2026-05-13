"""Tkinter settings window — runs as its own process for macOS main-thread safety.

Invoke with: `python -m src.settings_window`
The tray app spawns this as a subprocess so it has its own main thread for Tk.
"""

import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import httpx

from . import APP_NAME
from .config import Config
from .paths import default_zoom_folder


WINDOW_TITLE = f"{APP_NAME} — הגדרות"
ENTRY_WIDTH = 50
PAD = 8


def _set_rtl_alignment(widget):
    try:
        widget.configure(justify="right")
    except tk.TclError:
        pass


def main() -> None:
    cfg = Config.load()

    root = tk.Tk()
    root.title(WINDOW_TITLE)
    root.geometry("560x420")
    root.resizable(False, False)
    try:
        root.tk.call("tk", "scaling", 1.4)
    except tk.TclError:
        pass

    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)

    employee_var = tk.StringVar(value=cfg.employee_name)
    api_key_var = tk.StringVar(value=cfg.api_key)
    server_var = tk.StringVar(value=cfg.server_url)
    folder_var = tk.StringVar(value=cfg.watch_folder or str(default_zoom_folder()))
    interval_var = tk.StringVar(value=str(cfg.scan_interval_minutes))
    move_var = tk.BooleanVar(value=cfg.move_after_upload)

    def row(label_text: str, widget: tk.Widget, row_idx: int) -> None:
        ttk.Label(frame, text=label_text).grid(row=row_idx, column=0, sticky="e", padx=(0, 8), pady=4)
        widget.grid(row=row_idx, column=1, sticky="we", pady=4)

    name_entry = ttk.Entry(frame, textvariable=employee_var, width=ENTRY_WIDTH); _set_rtl_alignment(name_entry)
    row("שם העובדת:", name_entry, 0)

    key_entry = ttk.Entry(frame, textvariable=api_key_var, width=ENTRY_WIDTH, show="•")
    row("API key:", key_entry, 1)

    server_entry = ttk.Entry(frame, textvariable=server_var, width=ENTRY_WIDTH)
    row("כתובת השרת:", server_entry, 2)

    folder_frame = ttk.Frame(frame)
    folder_entry = ttk.Entry(folder_frame, textvariable=folder_var, width=ENTRY_WIDTH - 8)
    folder_entry.pack(side="left", fill="x", expand=True)
    def _browse():
        chosen = filedialog.askdirectory(title="בחר תיקיית הקלטות זום", initialdir=folder_var.get() or str(default_zoom_folder()))
        if chosen:
            folder_var.set(chosen)
    ttk.Button(folder_frame, text="עיון…", command=_browse).pack(side="left", padx=(6, 0))
    row("תיקיית הקלטות:", folder_frame, 3)

    interval_entry = ttk.Entry(frame, textvariable=interval_var, width=8)
    row("סריקה כל (דקות):", interval_entry, 4)

    move_check = ttk.Checkbutton(frame, text="להעביר ל-uploaded/ אחרי העלאה מוצלחת", variable=move_var)
    move_check.grid(row=5, column=1, sticky="w", pady=8)

    status_label = ttk.Label(frame, text="", foreground="gray")
    status_label.grid(row=6, column=0, columnspan=2, sticky="we", pady=(4, 4))

    def _test():
        url = server_var.get().rstrip("/")
        key = api_key_var.get()
        if not url:
            status_label.config(text="הזן URL לפני בדיקה", foreground="orange")
            return
        status_label.config(text="בודק…", foreground="gray")
        root.update_idletasks()
        try:
            with httpx.Client(timeout=10.0) as c:
                r = c.get(f"{url}/health")
                if r.status_code != 200:
                    status_label.config(text=f"❌ /health {r.status_code}", foreground="red")
                    return
                if key:
                    r = c.get(f"{url}/jobs", headers={"X-API-Key": key})
                    if r.status_code != 200:
                        status_label.config(text=f"❌ Auth: {r.status_code}", foreground="red")
                        return
                    jobs = r.json()
                    status_label.config(text=f"✓ חיבור תקין — {len(jobs)} jobs קודמים", foreground="green")
                else:
                    status_label.config(text="✓ /health תקין (לא נבדק API key)", foreground="green")
        except Exception as e:
            status_label.config(text=f"❌ {str(e)[:80]}", foreground="red")

    def _save_and_close():
        try:
            interval = int(interval_var.get())
            if interval < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("שגיאה", "מרווח סריקה חייב להיות מספר חיובי")
            return

        cfg.employee_name = employee_var.get().strip()
        cfg.api_key = api_key_var.get().strip()
        cfg.server_url = server_var.get().strip().rstrip("/")
        cfg.watch_folder = folder_var.get().strip()
        cfg.scan_interval_minutes = interval
        cfg.move_after_upload = bool(move_var.get())

        if not cfg.is_configured():
            messagebox.showerror("חסרים שדות", "כל ארבעת השדות (שם, API key, שרת, תיקייה) חייבים להיות מלאים")
            return

        cfg.save()
        root.destroy()

    btn_frame = ttk.Frame(frame)
    btn_frame.grid(row=7, column=0, columnspan=2, sticky="we", pady=(12, 0))
    ttk.Button(btn_frame, text="בדוק חיבור", command=_test).pack(side="right", padx=4)
    ttk.Button(btn_frame, text="שמירה", command=_save_and_close).pack(side="right", padx=4)
    ttk.Button(btn_frame, text="ביטול", command=root.destroy).pack(side="right", padx=4)

    frame.columnconfigure(1, weight=1)
    root.mainloop()


if __name__ == "__main__":
    main()
    sys.exit(0)
