import os
import threading
import ctypes
from ctypes import wintypes
import tkinter as tk
from tkinter import ttk, messagebox
import cv2
import numpy as np

from palettes import get_available_palettes

try:
    import psutil
except ImportError:
    psutil = None  # Auto PID detection disabled; manual PID entry still works

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------------------------------------------------------------------
# Video modes and geometry
# ---------------------------------------------------------------------

MODE_VGA256 = "VGA 256 colors (13h / Mode X)"
MODE_EGA16 = "EGA 16 colors (planar)"
MODES = [MODE_VGA256, MODE_EGA16]

# Default geometry per mode: (width in pixels, height in lines, bytes per line as the game sees it)
# VGA 256: one byte per pixel, so 320 bytes per line.
# EGA 16:  one bit per pixel in each of the 4 planes, so 40 bytes per line per plane.
MODE_DEFAULTS = {
    MODE_VGA256: (320, 200, 320),
    MODE_EGA16: (320, 200, 40),
}

# DOSBox keeps the 4 VGA/EGA planes interleaved: every address holds 4 bytes, one per plane.
# For EGA this means one line of the game uses 4 x stride bytes in memory.
PLANES = 4

MAX_READ = 0x40000          # never read more than 256 KB per frame
DISPLAY_SCALE = 3           # 320 pixels -> 960 on screen
ASPECT_43 = 1.2             # 320x200 was shown on 4:3 monitors: pixels are 1.2x taller than wide

# Standard EGA colors, in BGR order like the rest of this tool (OpenCV)
EGA_PALETTE_BGR = np.array([
    (0x00, 0x00, 0x00), (0xAA, 0x00, 0x00), (0x00, 0xAA, 0x00), (0xAA, 0xAA, 0x00),
    (0x00, 0x00, 0xAA), (0xAA, 0x00, 0xAA), (0x00, 0x55, 0xAA), (0xAA, 0xAA, 0xAA),
    (0x55, 0x55, 0x55), (0xFF, 0x55, 0x55), (0x55, 0xFF, 0x55), (0xFF, 0xFF, 0x55),
    (0x55, 0x55, 0xFF), (0xFF, 0x55, 0xFF), (0x55, 0xFF, 0xFF), (0xFF, 0xFF, 0xFF),
], dtype=np.float64)


def bytes_needed(mode, height, stride):
    """How many bytes one frame occupies in DOSBox video memory."""
    if mode == MODE_EGA16:
        return min(MAX_READ, height * stride * PLANES)
    return min(MAX_READ, height * stride)


def decode_vga256(data, width, height, stride):
    """Chunky 256-color pixels, `stride` bytes per line. Returns a (height, width) array of color indices."""
    data = np.frombuffer(data, dtype=np.uint8)
    lines = min(height, len(data) // stride)
    frame = np.zeros((height, width), dtype=np.uint8)
    if lines <= 0:
        return frame
    rows = data[:lines * stride].reshape((lines, stride))
    cols = min(width, stride)
    frame[:lines, :cols] = rows[:, :cols]
    return frame


def decode_ega16(data, width, height, stride):
    """
    EGA planar 16-color pixels as DOSBox stores them: 4 interleaved plane bytes per address,
    `stride` addresses per line, 8 pixels per address (most significant bit = leftmost pixel).
    Returns a (height, width) array of color indices 0-15.
    """
    data = np.frombuffer(data, dtype=np.uint8)
    line_bytes = stride * PLANES
    lines = min(height, len(data) // line_bytes)
    frame = np.zeros((height, width), dtype=np.uint8)
    if lines <= 0:
        return frame
    planes = data[:lines * line_bytes].reshape((lines, stride, PLANES))
    pixels = np.zeros((lines, stride * 8), dtype=np.uint8)
    for p in range(PLANES):
        pixels |= np.unpackbits(planes[:, :, p], axis=1) << p
    cols = min(width, pixels.shape[1])
    frame[:lines, :cols] = pixels[:, :cols]
    return frame


def find_dosbox_processes():
    """Returns a list of (pid, name) for every running process whose name contains 'dosbox'."""
    if psutil is None:
        return []
    found = []
    for proc in psutil.process_iter(["pid", "name"]):
        name = proc.info["name"] or ""
        if "dosbox" in name.lower():
            found.append((proc.info["pid"], name))
    return found


class DOSBoxVGAApp:
    def __init__(self, root):
        self.root = root
        self.root.title("DOSBox-X Live VGA Control Panel")
        self.root.geometry("480x620")
        self.root.resizable(False, False)

        self.is_running = False
        self.worker_thread = None
        self.current_addr = 0

        # Geometry shared with the stream thread (plain attributes, updated from the GUI)
        self.mode = MODE_VGA256
        self.geo_width, self.geo_height, self.geo_stride = MODE_DEFAULTS[MODE_VGA256]
        self.aspect_43 = True

        # Load available palettes
        self.palette_dict = get_available_palettes()
        default_name = list(self.palette_dict.keys())[0]
        self.base_palette = self.palette_dict[default_name].copy()
        self.active_palette = self.base_palette.astype(np.uint8)
        self.active_ega_palette = EGA_PALETTE_BGR.astype(np.uint8)

        self._build_ui()
        self.refresh_pid_list()
        self.root.protocol("WM_DELETE_WINDOW", self.on_exit)

    def _build_ui(self):
        pad_opts = {"padx": 10, "pady": 4}

        # PID selector (auto-detected DOSBox processes, manual typing also allowed)
        frame_pid = ttk.Frame(self.root)
        frame_pid.pack(fill="x", **pad_opts)
        ttk.Label(frame_pid, text="DOSBox PID:", width=16).pack(side="left")
        self.btn_refresh_pid = ttk.Button(frame_pid, text="Refresh", width=8, command=self.refresh_pid_list)
        self.btn_refresh_pid.pack(side="right", padx=(4, 0))
        self.combo_pid = ttk.Combobox(frame_pid)
        self.combo_pid.pack(side="right", expand=True, fill="x")

        # Offset / target address input field
        frame_addr = ttk.Frame(self.root)
        frame_addr.pack(fill="x", **pad_opts)
        ttk.Label(frame_addr, text="Target Address:", width=16).pack(side="left")
        self.entry_addr = ttk.Entry(frame_addr)
        self.entry_addr.insert(0, "0x0 + 0xA0000")
        self.entry_addr.pack(side="right", expand=True, fill="x")

        # Video mode and geometry
        frame_geo = ttk.LabelFrame(self.root, text="Video Mode & Geometry")
        frame_geo.pack(fill="x", padx=10, pady=6)

        f_mode = ttk.Frame(frame_geo)
        f_mode.pack(fill="x", padx=5, pady=2)
        ttk.Label(f_mode, text="Mode:", width=10).pack(side="left")
        self.var_mode = tk.StringVar(value=MODE_VGA256)
        combo_mode = ttk.Combobox(f_mode, textvariable=self.var_mode, values=MODES, state="readonly")
        combo_mode.pack(side="right", expand=True, fill="x")
        combo_mode.bind("<<ComboboxSelected>>", self.on_mode_change)

        f_size = ttk.Frame(frame_geo)
        f_size.pack(fill="x", padx=5, pady=2)
        self.var_width = tk.IntVar(value=self.geo_width)
        self.var_height = tk.IntVar(value=self.geo_height)
        self.var_stride = tk.IntVar(value=self.geo_stride)
        for label, var, low, high, step in (("Width", self.var_width, 8, 1024, 8),
                                            ("Height", self.var_height, 8, 768, 1),
                                            ("Stride", self.var_stride, 1, 2048, 1)):
            ttk.Label(f_size, text=label).pack(side="left", padx=(0, 2))
            sb = ttk.Spinbox(f_size, textvariable=var, from_=low, to=high, increment=step, width=6,
                             command=self.on_geometry_change)
            sb.pack(side="left", padx=(0, 8))
            sb.bind("<Return>", lambda _e: self.on_geometry_change())
            sb.bind("<FocusOut>", lambda _e: self.on_geometry_change())

        f_opts = ttk.Frame(frame_geo)
        f_opts.pack(fill="x", padx=5, pady=2)
        self.var_aspect = tk.BooleanVar(value=True)
        ttk.Checkbutton(f_opts, text="4:3 aspect (tall pixels)", variable=self.var_aspect,
                        command=self.on_geometry_change).pack(side="left")
        ttk.Button(f_opts, text="Reset", width=7, command=self.reset_geometry).pack(side="right")

        ttk.Label(frame_geo, text="Stride = bytes per line as the game sees it (VGA: 320, EGA: 40).\n"
                                  "A slanted image means the stride is wrong: use [ and ] to fix it.",
                  foreground="gray", justify="left").pack(anchor="w", padx=5, pady=(0, 4))

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
            "W / S : +/- 10 lines\n"
            "D / A : +/- 1 line   |   X / Z : +/- 1 byte (fine shift)\n"
            "[ / ] : stride -/+ 1   |   , / . : width -/+ 8\n"
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

    # ----- Geometry -----

    def on_mode_change(self, event=None):
        self.mode = self.var_mode.get()
        self.reset_geometry()

    def reset_geometry(self):
        width, height, stride = MODE_DEFAULTS[self.var_mode.get()]
        self.var_width.set(width)
        self.var_height.set(height)
        self.var_stride.set(stride)
        self.on_geometry_change()

    def on_geometry_change(self):
        """Copies the GUI values into plain attributes the stream thread reads."""
        try:
            width = max(8, int(self.var_width.get()))
            height = max(1, int(self.var_height.get()))
            stride = max(1, int(self.var_stride.get()))
        except (tk.TclError, ValueError):
            return
        self.mode = self.var_mode.get()
        self.geo_width, self.geo_height, self.geo_stride = width, height, stride
        self.aspect_43 = bool(self.var_aspect.get())

    def set_geometry_from_stream(self, width, stride):
        """Called on the GUI thread after a key press in the OpenCV window changed the geometry."""
        self.var_width.set(width)
        self.var_stride.set(stride)

    # ----- Process selection -----

    def refresh_pid_list(self):
        """Fills the PID dropdown with running DOSBox processes. Selects it automatically if only one is found."""
        if psutil is None:
            self.combo_pid["values"] = []
            self.lbl_status.config(text="Status: psutil not installed, enter PID manually", foreground="orange")
            return

        processes = find_dosbox_processes()
        items = [f"{pid} - {name}" for pid, name in processes]
        self.combo_pid["values"] = items

        if len(items) == 1:
            self.combo_pid.set(items[0])
            self.lbl_status.config(text=f"Status: Found {processes[0][1]} (PID {processes[0][0]})",
                                   foreground="blue")
        elif len(items) > 1:
            self.combo_pid.set(items[0])
            self.lbl_status.config(text=f"Status: {len(items)} DOSBox processes found, choose one",
                                   foreground="orange")
        else:
            self.combo_pid.set("")
            self.lbl_status.config(text="Status: DOSBox is not running", foreground="red")

    def get_selected_pid(self):
        """Accepts either a dropdown item ('12345 - dosbox-x.exe') or a plain typed number."""
        text = self.combo_pid.get().strip()
        return int(text.split(" - ")[0].strip())

    # ----- Palette -----

    def on_palette_change(self, event=None):
        selected = self.combo_palettes.get()
        if selected in self.palette_dict:
            self.base_palette = self.palette_dict[selected].copy()
            self.update_live_palette()

    def update_live_palette(self, event=None):
        r_scale = self.slider_r.get() / 255.0
        g_scale = self.slider_g.get() / 255.0
        b_scale = self.slider_b.get() / 255.0
        gains = np.array([b_scale, g_scale, r_scale])  # array format is [B, G, R]

        self.active_palette = np.clip(self.base_palette * gains, 0, 255).astype(np.uint8)
        self.active_ega_palette = np.clip(EGA_PALETTE_BGR * gains, 0, 255).astype(np.uint8)

    # ----- Address -----

    def parse_address(self, addr_str):
        """Adds up hex parts separated by '+'. A part can also be segment:offset ('A000:0000' = 0xA0000)."""
        total = 0
        for part in addr_str.strip().replace(" ", "").split("+"):
            if not part:
                continue
            if ":" in part:
                seg, off = part.split(":", 1)
                total += int(seg, 16) * 16 + int(off, 16)
            else:
                total += int(part, 16)
        return total

    def update_address_entry(self, new_addr):
        self.entry_addr.delete(0, tk.END)
        self.entry_addr.insert(0, hex(new_addr))

    # ----- Streaming -----

    def start_stream(self):
        try:
            pid = self.get_selected_pid()
        except ValueError:
            messagebox.showerror("Error", "Please select a DOSBox process or enter a valid PID!")
            return

        try:
            self.current_addr = self.parse_address(self.entry_addr.get().strip())
        except Exception:
            messagebox.showerror("Error", "Invalid address format (Example: 0x1234 + 0xA0000)")
            return

        self.on_geometry_change()
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

    def render_frame(self, raw):
        """Decodes one frame with the current mode and geometry and scales it for display."""
        mode, width, height, stride = self.mode, self.geo_width, self.geo_height, self.geo_stride
        if mode == MODE_EGA16:
            indices = decode_ega16(raw, width, height, stride)
            img = self.active_ega_palette[indices & 0x0F]
        else:
            indices = decode_vga256(raw, width, height, stride)
            img = self.active_palette[indices]

        out_w = width * DISPLAY_SCALE
        out_h = int(round(height * DISPLAY_SCALE * (ASPECT_43 if self.aspect_43 else 1.0)))
        return cv2.resize(img, (out_w, out_h), interpolation=cv2.INTER_NEAREST)

    def line_bytes(self):
        """Bytes one line occupies in DOSBox video memory (EGA planes are interleaved)."""
        return self.geo_stride * (PLANES if self.mode == MODE_EGA16 else 1)

    def _stream_loop(self, pid):
        h_process = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h_process:
            self.root.after(0, self.stop_stream)
            self.root.after(0, lambda: messagebox.showerror("Error", f"Could not open process PID {pid}!"))
            return

        window_name = "DOSBox-X Live VGA Stream"

        # Make the OpenCV window freely resizable by user
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window_name, 960, 720)

        c_buffer = (ctypes.c_char * MAX_READ)()
        bytes_read = ctypes.c_size_t()

        while self.is_running:
            size = bytes_needed(self.mode, self.geo_height, self.geo_stride)
            success = kernel32.ReadProcessMemory(
                h_process,
                ctypes.c_void_p(self.current_addr),
                c_buffer,
                size,
                ctypes.byref(bytes_read)
            )

            if success:
                scaled = self.render_frame(c_buffer.raw[:size])
                info = (f"Addr: {hex(self.current_addr)}  {self.geo_width}x{self.geo_height}  "
                        f"stride {self.geo_stride}")
                cv2.putText(scaled, info, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                cv2.imshow(window_name, scaled)

            delay_val = int(self.slider_delay.get())
            key = cv2.waitKey(max(1, delay_val)) & 0xFF

            addr_changed = False
            geo_changed = False

            if key == ord("q") or key == 27:
                self.is_running = False
                break
            elif key in (ord("+"), ord("=")):
                self.slider_delay.set(min(100, delay_val + 1))
            elif key == ord("-"):
                self.slider_delay.set(max(1, delay_val - 1))
            elif key == ord("w"):
                self.current_addr += 10 * self.line_bytes()  # 10 lines
                addr_changed = True
            elif key == ord("s"):
                self.current_addr -= 10 * self.line_bytes()
                addr_changed = True
            elif key == ord("d"):
                self.current_addr += self.line_bytes()       # 1 line
                addr_changed = True
            elif key == ord("a"):
                self.current_addr -= self.line_bytes()
                addr_changed = True
            elif key == ord("x"):
                self.current_addr += 1                       # 1 byte
                addr_changed = True
            elif key == ord("z"):
                self.current_addr -= 1
                addr_changed = True
            elif key == ord("]"):
                self.geo_stride += 1
                geo_changed = True
            elif key == ord("["):
                self.geo_stride = max(1, self.geo_stride - 1)
                geo_changed = True
            elif key == ord("."):
                self.geo_width = min(1024, self.geo_width + 8)
                geo_changed = True
            elif key == ord(","):
                self.geo_width = max(8, self.geo_width - 8)
                geo_changed = True

            if addr_changed:
                self.root.after(0, self.update_address_entry, self.current_addr)
            if geo_changed:
                self.root.after(0, self.set_geometry_from_stream, self.geo_width, self.geo_stride)

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