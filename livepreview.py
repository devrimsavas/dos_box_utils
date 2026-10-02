import ctypes
import ctypes.wintypes as wt
import threading
import time
import tkinter as tk
from tkinter import ttk

import mss
from PIL import Image, ImageTk

# Ekran koordinatları ölçekli (125%, 150%) ekranlarda doğru çıksın diye
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

kernel32.OpenProcess.restype = wt.HANDLE

PREVIEW_MAX_W = 640
PREVIEW_MAX_H = 400
PREVIEW_INTERVAL_MS = 50  # ~20 FPS


def parse_address_input(addr_str):
    addr_str = addr_str.strip().replace(" ", "")
    total = 0
    for part in addr_str.split("+"):
        total += int(part, 16)
    return total


# ---------- Pencere bulma (PID -> pencere) ----------

EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def find_main_window(pid):
    """Verilen PID'e ait, görünür ve en büyük üst seviye pencereyi döndürür."""
    best = {"hwnd": None, "area": 0}

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        win_pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
        if win_pid.value != pid:
            return True
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = (rect.right - rect.left) * (rect.bottom - rect.top)
        if area > best["area"]:
            best["hwnd"] = hwnd
            best["area"] = area
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return best["hwnd"]


def get_client_area(hwnd):
    """Pencerenin başlık çubuğu ve kenarlıkları hariç iç alanını ekran koordinatlarıyla döndürür."""
    rect = wt.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    return {"left": pt.x, "top": pt.y, "width": width, "height": height}


# ---------- Hile satırı ----------

class CheatRow:
    def __init__(self, parent, row_num, app):
        self.app = app
        self.row_num = row_num
        self.is_frozen = False

        self.frame = ttk.Frame(parent)
        self.frame.pack(fill=tk.X, padx=5, pady=2)

        ttk.Label(self.frame, text=f"#{row_num}", width=3).pack(side=tk.LEFT, padx=2)

        self.ent_addr = ttk.Entry(self.frame, width=20)
        self.ent_addr.pack(side=tk.LEFT, padx=3)

        self.ent_bytes = ttk.Entry(self.frame, width=16)
        self.ent_bytes.pack(side=tk.LEFT, padx=3)

        self.btn_inject = ttk.Button(self.frame, text="Inject", width=8, command=self.inject_value)
        self.btn_inject.pack(side=tk.LEFT, padx=3)

        self.btn_freeze = ttk.Button(self.frame, text="Freeze", width=8, command=self.toggle_freeze)
        self.btn_freeze.pack(side=tk.LEFT, padx=3)

    def get_parsed_data(self):
        addr_str = self.ent_addr.get().strip()
        byte_str = self.ent_bytes.get().strip()
        if not addr_str or not byte_str:
            return None, None
        target_addr = parse_address_input(addr_str)
        byte_data = bytes.fromhex(byte_str.replace(" ", ""))
        return target_addr, byte_data

    def inject_value(self):
        if not self.app.h_process:
            return
        try:
            target_addr, byte_data = self.get_parsed_data()
            if target_addr is None:
                return
            bytes_written = ctypes.c_size_t()
            kernel32.WriteProcessMemory(
                self.app.h_process,
                ctypes.c_void_p(target_addr),
                byte_data,
                len(byte_data),
                ctypes.byref(bytes_written),
            )
        except Exception:
            pass

    def toggle_freeze(self):
        self.is_frozen = not self.is_frozen
        self.btn_freeze.config(text="Frozen" if self.is_frozen else "Freeze")


# ---------- Ana uygulama ----------

class DOSBoxTrainerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("DOSBox-X Memory Trainer")
        self.root.geometry("680x640")

        self.h_process = None
        self.hwnd = None
        self.running = False
        self.freeze_thread = None

        self.sct = mss.mss()          # ekran yakalayıcı (ana thread'de kullanılıyor)
        self.preview_image = None     # Tkinter görüntüyü silmesin diye referans tutuluyor

        self.setup_ui()

    def setup_ui(self):
        top_frame = ttk.Frame(self.root, padding=5)
        top_frame.pack(fill=tk.X)

        ttk.Label(top_frame, text="PID:").pack(side=tk.LEFT, padx=2)
        self.ent_pid = ttk.Entry(top_frame, width=10)
        self.ent_pid.pack(side=tk.LEFT, padx=4)

        self.btn_conn = ttk.Button(top_frame, text="Attach", width=10, command=self.toggle_process)
        self.btn_conn.pack(side=tk.LEFT, padx=4)

        self.lbl_status = ttk.Label(top_frame, text="Ready", foreground="gray")
        self.lbl_status.pack(side=tk.LEFT, padx=10)

        header_frame = ttk.Frame(self.root, padding=(5, 5, 5, 0))
        header_frame.pack(fill=tk.X)
        ttk.Label(header_frame, text="#", width=3).pack(side=tk.LEFT, padx=2)
        ttk.Label(header_frame, text="Address (Hex / Base+Offset)", width=20).pack(side=tk.LEFT, padx=3)
        ttk.Label(header_frame, text="Bytes (Hex)", width=16).pack(side=tk.LEFT, padx=3)

        self.rows_frame = ttk.Frame(self.root, padding=5)
        self.rows_frame.pack(fill=tk.X)

        self.rows = [CheatRow(self.rows_frame, i, self) for i in range(1, 5)]

        for i in range(4):
            self.root.bind(f"<KP_{i + 1}>", lambda e, idx=i: self.rows[idx].inject_value())

        # Canlı önizleme alanı
        preview_frame = ttk.LabelFrame(self.root, text="Live Preview", padding=5)
        preview_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.lbl_preview = tk.Label(preview_frame, text="Not attached", bg="black", fg="gray")
        self.lbl_preview.pack(fill=tk.BOTH, expand=True)

    # ----- Bağlanma / ayrılma -----

    def toggle_process(self):
        if not self.h_process:
            try:
                pid = int(self.ent_pid.get().strip())
            except ValueError:
                return

            desired_access = (
                PROCESS_QUERY_INFORMATION
                | PROCESS_VM_READ
                | PROCESS_VM_WRITE
                | PROCESS_VM_OPERATION
            )
            self.h_process = kernel32.OpenProcess(desired_access, False, pid)
            if not self.h_process:
                self.lbl_status.config(text="Attach Failed", foreground="red")
                return

            self.hwnd = find_main_window(pid)

            self.running = True
            self.btn_conn.config(text="Detach")
            self.lbl_status.config(text=f"Attached ({pid})", foreground="green")

            self.freeze_thread = threading.Thread(target=self.freeze_loop, daemon=True)
            self.freeze_thread.start()

            self.update_preview()
        else:
            self.running = False
            if self.h_process:
                kernel32.CloseHandle(self.h_process)
                self.h_process = None
            self.hwnd = None
            self.btn_conn.config(text="Attach")
            self.lbl_status.config(text="Ready", foreground="gray")
            self.preview_image = None
            self.lbl_preview.config(image="", text="Not attached")

    # ----- Canlı önizleme -----

    def update_preview(self):
        if not self.running:
            return

        if not self.hwnd or not user32.IsWindow(self.hwnd):
            self.lbl_preview.config(image="", text="DOSBox window not found")
        elif user32.IsIconic(self.hwnd):
            self.lbl_preview.config(image="", text="DOSBox window is minimized")
        else:
            area = get_client_area(self.hwnd)
            if area:
                try:
                    shot = self.sct.grab(area)
                    img = Image.frombytes("RGB", shot.size, shot.rgb)
                    img.thumbnail((PREVIEW_MAX_W, PREVIEW_MAX_H), Image.NEAREST)
                    self.preview_image = ImageTk.PhotoImage(img)
                    self.lbl_preview.config(image=self.preview_image, text="")
                except Exception:
                    pass

        self.root.after(PREVIEW_INTERVAL_MS, self.update_preview)

    # ----- Freeze döngüsü -----

    def freeze_loop(self):
        bytes_written = ctypes.c_size_t()
        while self.running:
            for row in self.rows:
                if row.is_frozen and self.h_process:
                    try:
                        target_addr, byte_data = row.get_parsed_data()
                        if target_addr is not None and byte_data:
                            kernel32.WriteProcessMemory(
                                self.h_process,
                                ctypes.c_void_p(target_addr),
                                byte_data,
                                len(byte_data),
                                ctypes.byref(bytes_written),
                            )
                    except Exception:
                        pass
            time.sleep(0.05)


if __name__ == "__main__":
    root = tk.Tk()
    app = DOSBoxTrainerApp(root)
    root.mainloop()