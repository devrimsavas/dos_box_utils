import ctypes
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def parse_address_input(addr_str):
    addr_str = addr_str.strip().replace(" ", "")
    total = 0
    parts = addr_str.split("+")
    for part in parts:
        total += int(part, 16)
    return total


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


class DOSBoxTrainerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("DOSBox-X Memory Trainer")
        self.root.geometry("450x240")
        self.root.resizable(False, False)

        self.h_process = None
        self.running = False
        self.freeze_thread = None

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
        self.rows_frame.pack(fill=tk.BOTH, expand=True)

        self.rows = [CheatRow(self.rows_frame, i, self) for i in range(1, 5)]

        self.root.bind("<KP_1>", lambda e: self.rows[0].inject_value())
        self.root.bind("<KP_2>", lambda e: self.rows[1].inject_value())
        self.root.bind("<KP_3>", lambda e: self.rows[2].inject_value())
        self.root.bind("<KP_4>", lambda e: self.rows[3].inject_value())

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

            self.running = True
            self.btn_conn.config(text="Detach")
            self.lbl_status.config(text=f"Attached ({pid})", foreground="green")

            self.freeze_thread = threading.Thread(target=self.freeze_loop, daemon=True)
            self.freeze_thread.start()
        else:
            self.running = False
            if self.h_process:
                kernel32.CloseHandle(self.h_process)
                self.h_process = None
            self.btn_conn.config(text="Attach")
            self.lbl_status.config(text="Ready", foreground="gray")

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