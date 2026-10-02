import os
import threading
import ctypes
from ctypes import wintypes
import tkinter as tk
from tkinter import ttk, messagebox
import cv2
import numpy as np

from palettes import get_available_palettes

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class DOSBoxVGAApp:
    def __init__(self, root):
        self.root = root
        self.root.title("DOSBox-X Live VGA Control Panel")
        self.root.geometry("470x560")
        self.root.resizable(False, False)

        self.is_running = False
        self.worker_thread = None
        self.current_addr = 0

        # Load available palettes
        self.palette_dict = get_available_palettes()
        default_name = list(self.palette_dict.keys())[0]
        self.base_palette = self.palette_dict[default_name].copy()
        self.active_palette = self.base_palette.astype(np.uint8)

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_exit)

    def _build_ui(self):
        pad_opts = {"padx": 10, "pady": 4}

        # PID input field
        frame_pid = ttk.Frame(self.root)
        frame_pid.pack(fill="x", **pad_opts)
        ttk.Label(frame_pid, text="DOSBox PID:", width=16).pack(side="left")
        self.entry_pid = ttk.Entry(frame_pid)
        self.entry_pid.pack(side="right", expand=True, fill="x")

        # Offset / target address input field
        frame_addr = ttk.Frame(self.root)
        frame_addr.pack(fill="x", **pad_opts)
        ttk.Label(frame_addr, text="Target Address:", width=16).pack(side="left")
        self.entry_addr = ttk.Entry(frame_addr)
        self.entry_addr.insert(0, "0x0 + 0xA0000")
        self.entry_addr.pack(side="right", expand=True, fill="x")

        # Palette selector dropdown
        frame_pal = ttk.Frame(self.root)
        frame_pal.pack(fill="x", **pad_opts)
        ttk.Label(frame_pal, text="Palette Profile:", width=16).pack(side="left")
        self.combo_palettes = ttk.Combobox(
            frame_pal,
            state="readonly",
            values=list(self.palette_dict.keys())
        )
        self.combo_palettes.current(0)
        self.combo_palettes.pack(side="right", expand=True, fill="x")
        self.combo_palettes.bind("<<ComboboxSelected>>", self.on_palette_change)

        # Live RGB gain adjustment sliders
        frame_rgb = ttk.LabelFrame(self.root, text="Live Color Adjustment (RGB Gain: 0 - 255)")
        frame_rgb.pack(fill="x", padx=10, pady=6)

        # Red channel slider
        f_r = ttk.Frame(frame_rgb)
        f_r.pack(fill="x", padx=5, pady=2)
        ttk.Label(f_r, text="R (Red):", width=10, foreground="red").pack(side="left")
        self.slider_r = ttk.Scale(f_r, from_=0, to=255, orient="horizontal")
        self.slider_r.set(255)
        self.slider_r.pack(side="right", expand=True, fill="x")

        # Green channel slider
        f_g = ttk.Frame(frame_rgb)
        f_g.pack(fill="x", padx=5, pady=2)
        ttk.Label(f_g, text="G (Green):", width=10, foreground="green").pack(side="left")
        self.slider_g = ttk.Scale(f_g, from_=0, to=255, orient="horizontal")
        self.slider_g.set(255)
        self.slider_g.pack(side="right", expand=True, fill="x")

        # Blue channel slider
        f_b = ttk.Frame(frame_rgb)
        f_b.pack(fill="x", padx=5, pady=2)
        ttk.Label(f_b, text="B (Blue):", width=10, foreground="blue").pack(side="left")
        self.slider_b = ttk.Scale(f_b, from_=0, to=255, orient="horizontal")
        self.slider_b.set(255)
        self.slider_b.pack(side="right", expand=True, fill="x")

        # Attach callbacks safely after all three sliders are created to avoid AttributeError
        self.slider_r.config(command=self.update_live_palette)
        self.slider_g.config(command=self.update_live_palette)
        self.slider_b.config(command=self.update_live_palette)

        # Frame delay slider (ms)
        frame_delay = ttk.Frame(self.root)
        frame_delay.pack(fill="x", **pad_opts)
        ttk.Label(frame_delay, text="Delay (ms):", width=16).pack(side="left")
        self.slider_delay = ttk.Scale(frame_delay, from_=1, to=100, orient="horizontal")
        self.slider_delay.set(16)
        self.slider_delay.pack(side="right", expand=True, fill="x")

        # Keymap documentation label
        frame_info = ttk.LabelFrame(self.root, text="Keyboard Shortcuts (OpenCV Window)")
        frame_info.pack(fill="x", padx=10, pady=4)
        lbl_text = (
            "W / S : +/- 3200 bytes (10 scanlines)\n"
            "D / A : +/- 1 pixel (fine shift)\n"
            "X / Z : +/- 1 pixel (fine shift)\n"
            "+ / - : Frame delay (ms) | Q : Stop stream"
        )
        ttk.Label(frame_info, text=lbl_text, justify="center").pack(pady=4)

        # Status indicator
        self.lbl_status = ttk.Label(self.root, text="Status: Ready", foreground="blue")
        self.lbl_status.pack(pady=4)

        # Bottom control buttons
        frame_btn = ttk.Frame(self.root)
        frame_btn.pack(fill="x", pady=6)

        self.btn_run = ttk.Button(frame_btn, text="RUN", command=self.start_stream)
        self.btn_run.pack(side="left", expand=True, padx=5)

        self.btn_stop = ttk.Button(frame_btn, text="STOP", command=self.stop_stream, state="disabled")
        self.btn_stop.pack(side="left", expand=True, padx=5)

        self.btn_exit = ttk.Button(frame_btn, text="EXIT", command=self.on_exit)
        self.btn_exit.pack(side="right", expand=True, padx=5)

    def on_palette_change(self, event=None):
        selected = self.combo_palettes.get()
        if selected in self.palette_dict:
            self.base_palette = self.palette_dict[selected].copy()
            self.update_live_palette()

    def update_live_palette(self, event=None):
        r_scale = self.slider_r.get() / 255.0
        g_scale = self.slider_g.get() / 255.0
        b_scale = self.slider_b.get() / 255.0

        # Channel mapping: array format is [B, G, R]
        scaled = np.empty_like(self.base_palette)
        scaled[:, 0] = np.clip(self.base_palette[:, 0] * b_scale, 0, 255)
        scaled[:, 1] = np.clip(self.base_palette[:, 1] * g_scale, 0, 255)
        scaled[:, 2] = np.clip(self.base_palette[:, 2] * r_scale, 0, 255)
        self.active_palette = scaled.astype(np.uint8)

    def parse_address(self, addr_str):
        total = 0
        for part in addr_str.strip().replace(" ", "").split("+"):
            total += int(part, 16)
        return total

    def update_address_entry(self, new_addr):
        self.entry_addr.delete(0, tk.END)
        self.entry_addr.insert(0, hex(new_addr))

    def start_stream(self):
        try:
            pid = int(self.entry_pid.get().strip())
        except ValueError:
            messagebox.showerror("Error", "Please provide a valid integer PID!")
            return

        try:
            self.current_addr = self.parse_address(self.entry_addr.get().strip())
        except Exception:
            messagebox.showerror("Error", "Invalid address format (Example: 0x1234 + 0xA0000)")
            return

        self.update_live_palette()
        self.is_running = True
        self.btn_run.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.lbl_status.config(text="Status: Streaming...", foreground="green")

        self.worker_thread = threading.Thread(target=self._stream_loop, args=(pid,), daemon=True)
        self.worker_thread.start()

    def stop_stream(self):
        self.is_running = False
        self.btn_run.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.lbl_status.config(text="Status: Stopped", foreground="red")

    def _stream_loop(self, pid):
        h_process = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h_process:
            self.stop_stream()
            messagebox.showerror("Error", f"Could not open process PID {pid}!")
            return

        window_name = "DOSBox-X Live VGA Stream"

        # Make the OpenCV window freely resizable by user
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window_name, 960, 720)

        buf_size = 64000
        c_buffer = (ctypes.c_char * buf_size)()
        bytes_read = ctypes.c_size_t()

        while self.is_running:
            success = kernel32.ReadProcessMemory(
                h_process,
                ctypes.c_void_p(self.current_addr),
                c_buffer,
                buf_size,
                ctypes.byref(bytes_read)
            )

            if success:
                data = np.frombuffer(c_buffer.raw, dtype=np.uint8)
                if len(data) >= 64000:
                    frame = data[:64000].reshape((200, 320))
                    img = self.active_palette[frame]

                    # Scale using nearest-neighbor to maintain retro crispness
                    scaled = cv2.resize(img, (960, 600), interpolation=cv2.INTER_NEAREST)

                    cv2.putText(scaled, f"Addr: {hex(self.current_addr)}", (10, 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
                    cv2.imshow(window_name, scaled)

            delay_val = int(self.slider_delay.get())
            key = cv2.waitKey(max(1, delay_val)) & 0xFF

            addr_changed = False

            if key == ord("q") or key == 27:
                self.is_running = False
                break
            elif key in (ord("+"), ord("=")):
                new_delay = min(100, delay_val + 1)
                self.slider_delay.set(new_delay)
            elif key == ord("-"):
                new_delay = max(1, delay_val - 1)
                self.slider_delay.set(new_delay)
            elif key == ord("w"):
                self.current_addr += 3200  # Shift up 10 scanlines
                addr_changed = True
            elif key == ord("s"):
                self.current_addr -= 3200  # Shift down 10 scanlines
                addr_changed = True
            elif key == ord("d"):
                self.current_addr += 1     # Forward 1 pixel
                addr_changed = True
            elif key == ord("a"):
                self.current_addr -= 1     # Backward 1 pixel
                addr_changed = True
            elif key == ord("x"):
                self.current_addr += 1     # Forward 1 pixel
                addr_changed = True
            elif key == ord("z"):
                self.current_addr -= 1     # Backward 1 pixel
                addr_changed = True

            if addr_changed:
                self.root.after(0, self.update_address_entry, self.current_addr)

        kernel32.CloseHandle(h_process)
        cv2.destroyAllWindows()
        self.root.after(0, self.stop_stream)

    def on_exit(self):
        self.is_running = False
        cv2.destroyAllWindows()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = DOSBoxVGAApp(root)
    root.mainloop()