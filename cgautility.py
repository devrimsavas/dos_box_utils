import ctypes
from collections import deque
import tkinter as tk
from tkinter import ttk, messagebox
import cv2
import numpy as np
from PIL import Image, ImageTk

# Windows API constants
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def parse_address_input(addr_str):
    addr_str = addr_str.strip().replace(" ", "")
    total = 0
    parts = addr_str.split("+")
    for part in parts:
        total += int(part, 16)
    return total


class CGALiveTunerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("DOSBox-X CGA Live Tuner & Palette Editor")
        self.root.geometry("1100x700")

        self.h_process = None
        self.current_target = 0
        self.last_valid_target = 0
        self.delay_ms = 16
        self.running = False

        self.buf_size = 0x4000  # 16 KB
        self.c_buffer = (ctypes.c_char * self.buf_size)()
        self.bytes_read = ctypes.c_size_t()

        # Flicker filter: 
        self.filter_size = 3
        self.history = deque(maxlen=self.filter_size)

        # Default CGA Palette 1 (High Intensity): Black, Cyan, Magenta, White (RGB)
        self.palette_rgb = [
            [0, 0, 0],        # Color 0
            [0, 255, 255],    # Color 1
            [255, 0, 255],    # Color 2
            [255, 255, 255],  # Color 3
        ]

        self.setup_ui()
        self.setup_keybindings()

    def setup_ui(self):
        # Left Panel: Controls & Palette Sliders
        control_frame = ttk.Frame(self.root, padding=10)
        control_frame.pack(side=tk.LEFT, fill=tk.Y)

        # Connection Group
        conn_group = ttk.LabelFrame(control_frame, text="Process Connection", padding=5)
        conn_group.pack(fill=tk.X, pady=5)

        ttk.Label(conn_group, text="DOSBox-X PID:").grid(row=0, column=0, sticky=tk.W)
        self.pid_entry = ttk.Entry(conn_group, width=15)
        self.pid_entry.grid(row=0, column=1, padx=5, pady=2)

        ttk.Label(conn_group, text="Start Address:").grid(row=1, column=0, sticky=tk.W)
        self.addr_entry = ttk.Entry(conn_group, width=15)
        self.addr_entry.grid(row=1, column=1, padx=5, pady=2)
        self.addr_entry.insert(0, "0xB800")

        self.btn_connect = ttk.Button(conn_group, text="Connect", command=self.toggle_connection)
        self.btn_connect.grid(row=2, column=0, columnspan=2, pady=5)

        # Navigation Group
        nav_group = ttk.LabelFrame(control_frame, text="Address Navigation (Keys: W/S, A/D, Z/X)", padding=5)
        nav_group.pack(fill=tk.X, pady=5)

        btn_row1 = ttk.Frame(nav_group)
        btn_row1.pack(fill=tk.X, pady=2)
        ttk.Button(btn_row1, text="-800 B (S)", width=10, command=lambda: self.adjust_addr(-800)).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row1, text="+800 B (W)", width=10, command=lambda: self.adjust_addr(800)).pack(side=tk.LEFT, padx=2)

        btn_row2 = ttk.Frame(nav_group)
        btn_row2.pack(fill=tk.X, pady=2)
        ttk.Button(btn_row2, text="-80 B (A)", width=10, command=lambda: self.adjust_addr(-80)).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row2, text="+80 B (D)", width=10, command=lambda: self.adjust_addr(80)).pack(side=tk.LEFT, padx=2)

        btn_row3 = ttk.Frame(nav_group)
        btn_row3.pack(fill=tk.X, pady=2)
        ttk.Button(btn_row3, text="-1 B (Z)", width=10, command=lambda: self.adjust_addr(-1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row3, text="+1 B (X)", width=10, command=lambda: self.adjust_addr(1)).pack(side=tk.LEFT, padx=2)

        # Flicker Filter Group
        filter_group = ttk.LabelFrame(control_frame, text="Flicker Filter (1 = off)", padding=5)
        filter_group.pack(fill=tk.X, pady=5)

        self.lbl_filter = ttk.Label(filter_group, text=f"Frames: {self.filter_size}", width=10)
        self.lbl_filter.pack(side=tk.LEFT)
        self.scale_filter = tk.Scale(
            filter_group,
            from_=1,
            to=9,
            resolution=2,  # 1, 3, 5, 7, 9 (tek sayılar, beraberlik daha az olur)
            orient=tk.HORIZONTAL,
            showvalue=False,
            length=150,
            command=self.on_filter_change,
        )
        self.scale_filter.set(self.filter_size)
        self.scale_filter.pack(side=tk.LEFT, padx=5)

        # Palette Sliders Group
        palette_group = ttk.LabelFrame(control_frame, text="CGA Palette Colors (RGB)", padding=5)
        palette_group.pack(fill=tk.BOTH, expand=True, pady=5)

        self.color_previews = []
        color_labels = ["Color 0 (Background)", "Color 1 (Cyan)", "Color 2 (Magenta)", "Color 3 (White)"]

        for idx, name in enumerate(color_labels):
            box = ttk.LabelFrame(palette_group, text=name, padding=3)
            box.pack(fill=tk.X, pady=2)

            preview = tk.Canvas(box, width=30, height=20, relief=tk.RIDGE, bd=1)
            preview.grid(row=0, column=0, rowspan=3, padx=5)
            self.color_previews.append(preview)

            for c_idx, c_name in enumerate(["R", "G", "B"]):
                ttk.Label(box, text=c_name).grid(row=c_idx, column=1, padx=2)
                scale = tk.Scale(
                    box,
                    from_=0,
                    to=255,
                    orient=tk.HORIZONTAL,
                    showvalue=False,
                    length=120,
                    command=lambda val, col=idx, ch=c_idx: self.on_palette_slider_change(col, ch, val),
                )
                scale.set(self.palette_rgb[idx][c_idx])
                scale.grid(row=c_idx, column=2, padx=2)

            self.update_preview_box(idx)

        # Right Panel: Display Screen
        display_frame = ttk.Frame(self.root, padding=10)
        display_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        self.lbl_status = ttk.Label(display_frame, text="Status: Disconnected | Addr: 0x0")
        self.lbl_status.pack(anchor=tk.W, pady=2)

        self.canvas_screen = tk.Canvas(display_frame, bg="black", width=640, height=400)
        self.canvas_screen.pack(fill=tk.BOTH, expand=True)

    def setup_keybindings(self):
        # Global key press binding
        self.root.bind("<KeyPress>", self.on_key_press)

    def on_key_press(self, event):
        # Do not navigate if typing inside an Entry widget
        if isinstance(event.widget, (ttk.Entry, tk.Entry)):
            return

        key = event.char.lower()
        if key == "w":
            self.adjust_addr(800)
        elif key == "s":
            self.adjust_addr(-800)
        elif key == "d":
            self.adjust_addr(80)
        elif key == "a":
            self.adjust_addr(-80)
        elif key == "x":
            self.adjust_addr(1)
        elif key == "z":
            self.adjust_addr(-1)

    def on_palette_slider_change(self, color_idx, channel_idx, val):
        self.palette_rgb[color_idx][channel_idx] = int(val)
        self.update_preview_box(color_idx)

    def update_preview_box(self, color_idx):
        r, g, b = self.palette_rgb[color_idx]
        hex_color = f"#{r:02x}{g:02x}{b:02x}"
        self.color_previews[color_idx].configure(bg=hex_color)

    def on_filter_change(self, val):
        size = int(float(val))
        if size == self.filter_size:
            return
        self.filter_size = size
        # Mevcut son kareleri koruyarak geçmişi yeni boyuta göre yeniden oluştur
        self.history = deque(self.history, maxlen=size)
        self.lbl_filter.config(text=f"Frames: {size}")

    def adjust_addr(self, delta):
        if self.running:
            self.current_target += delta
            self.history.clear()  # farklı adresin kareleri karışmasın
            self.lbl_status.config(text=f"Status: Streaming | Addr: {hex(self.current_target)}")

    def toggle_connection(self):
        if not self.running:
            try:
                dosbox_pid = int(self.pid_entry.get().strip())
                self.current_target = parse_address_input(self.addr_entry.get().strip())
                self.last_valid_target = self.current_target
            except Exception as e:
                messagebox.showerror("Input Error", f"Invalid PID or Address: {e}")
                return

            self.h_process = kernel32.OpenProcess(
                PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, dosbox_pid
            )

            if not self.h_process:
                err = ctypes.get_last_error()
                messagebox.showerror("Error", f"Failed to open process! Error code: {err}")
                return

            self.history.clear()
            self.running = True
            self.btn_connect.config(text="Disconnect")
            self.root.after(self.delay_ms, self.update_frame)
        else:
            self.running = False
            if self.h_process:
                kernel32.CloseHandle(self.h_process)
                self.h_process = None
            self.btn_connect.config(text="Connect")
            self.lbl_status.config(text="Status: Disconnected | Addr: 0x0")

    def decode_cga_mode4(self, raw_bytes):
        """Ham CGA belleğini 200x320'lik renk indeksi (0-3) dizisine çevirir."""
        data = np.frombuffer(raw_bytes, dtype=np.uint8)
        if len(data) < 0x4000:
            return np.zeros((200, 320), dtype=np.uint8)

        even_bank = data[0x0000:0x1F40].reshape((100, 80))
        odd_bank = data[0x2000:0x3F40].reshape((100, 80))

        frame = np.zeros((200, 320), dtype=np.uint8)

        for i in range(4):
            shift = 6 - (i * 2)
            even_pixels = (even_bank >> shift) & 0x03
            odd_pixels = (odd_bank >> shift) & 0x03
            frame[0::2, i::4] = even_pixels
            frame[1::2, i::4] = odd_pixels

        return frame

    def filter_flicker(self, frame):
        """
        Son N okumada her piksel için en sık görülen renk indeksini seçer.
        Karakter birkaç okumada eksik çıksa bile çoğunlukta göründüğü için ekranda kalır.
        Beraberlikte en son okunan değer kazanır.
        """
        self.history.append(frame)
        if len(self.history) == 1:
            return frame

        stack = np.stack(self.history)  # (N, 200, 320)

        # Her renk indeksi (0-3) için kaç karede görüldüğünü say
        counts = np.stack([(stack == k).sum(axis=0) for k in range(4)]).astype(np.float32)

        # Beraberlik bozucu: en son karedeki değere yarım puan ekle
        latest = self.history[-1]
        for k in range(4):
            counts[k] += 0.5 * (latest == k)

        return counts.argmax(axis=0).astype(np.uint8)

    def update_frame(self):
        if not self.running:
            return

        success = kernel32.ReadProcessMemory(
            self.h_process,
            ctypes.c_void_p(self.current_target),
            self.c_buffer,
            self.buf_size,
            ctypes.byref(self.bytes_read),
        )

        if success:
            self.last_valid_target = self.current_target
            index_frame = self.decode_cga_mode4(self.c_buffer.raw)
            stable_frame = self.filter_flicker(index_frame)
            palette_arr = np.array(self.palette_rgb, dtype=np.uint8)
            rgb_frame = palette_arr[stable_frame]

            # Resize to fit canvas
            c_w = max(320, self.canvas_screen.winfo_width())
            c_h = max(200, self.canvas_screen.winfo_height())
            scaled_frame = cv2.resize(rgb_frame, (c_w, c_h), interpolation=cv2.INTER_NEAREST)

            img = Image.fromarray(scaled_frame)
            self.tk_img = ImageTk.PhotoImage(image=img)
            self.canvas_screen.delete("all")
            self.canvas_screen.create_image(0, 0, anchor=tk.NW, image=self.tk_img)

            self.lbl_status.config(text=f"Status: Streaming | Addr: {hex(self.current_target)}")
        else:
            self.current_target = self.last_valid_target
            self.lbl_status.config(text=f"Status: Memory Read Error! Reverted to: {hex(self.current_target)}")

        self.root.after(self.delay_ms, self.update_frame)


if __name__ == "__main__":
    root = tk.Tk()
    app = CGALiveTunerApp(root)
    root.mainloop()