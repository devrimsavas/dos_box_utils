"""
DOSBox Memory Diff Tool

Compares two memory snapshots (snap1.bin / snap2.bin) and lists every byte
that changed. Results can be filtered, sorted, copied and exported.
"""

import csv
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np

DEFAULT_SNAP1 = "c:/dosbox-x/snap1.bin"
DEFAULT_SNAP2 = "c:/dosbox-x/snap2.bin"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUTO_SAVE_FILE = os.path.join(BASE_DIR, "diff_results.txt")

MAX_ROWS_DISPLAYED = 5000  # the table gets slow above this; exports always include everything

CHANGE_ANY = "Any change"
CHANGE_UP = "Increased"
CHANGE_DOWN = "Decreased"
CHANGE_TO_ZERO = "Became zero"
CHANGE_FROM_ZERO = "Was zero"
CHANGE_DELTA = "Delta equals"
CHANGE_TYPES = [CHANGE_ANY, CHANGE_UP, CHANGE_DOWN, CHANGE_TO_ZERO, CHANGE_FROM_ZERO, CHANGE_DELTA]


# =====================================================================
# Core logic (no GUI) - easy to test and reuse
# =====================================================================

def load_snapshot(path):
    """Reads a binary snapshot file as an array of bytes."""
    return np.fromfile(path, dtype=np.uint8)


def compute_diff(data1, data2):
    """Returns (offsets, old_values, new_values) for every byte that differs."""
    size = min(len(data1), len(data2))
    a = data1[:size]
    b = data2[:size]
    offsets = np.nonzero(a != b)[0]
    return offsets, a[offsets], b[offsets]


def parse_value(text):
    """
    Parses a byte value or delta typed by the user.
    '0x1F' or '1Fh' -> hexadecimal, '31' -> decimal, '-3' -> negative. Empty -> None.
    """
    text = text.strip().lower().replace(" ", "")
    if not text:
        return None
    sign = 1
    if text[0] in "+-":
        sign = -1 if text[0] == "-" else 1
        text = text[1:]
    if text.startswith("0x"):
        return sign * int(text, 16)
    if text.endswith("h"):
        return sign * int(text[:-1], 16)
    return sign * int(text, 10)


def parse_offset(text):
    """Offsets are always hexadecimal, with or without the 0x prefix. Empty -> None."""
    text = text.strip().lower().replace(" ", "")
    if not text:
        return None
    if text.startswith("0x"):
        text = text[2:]
    return int(text, 16)


def build_filter_mask(offsets, old, new, offset_from=None, offset_to=None,
                      old_value=None, new_value=None, change_type=CHANGE_ANY, delta_value=None):
    """Returns a boolean mask selecting the differences that match every filter."""
    mask = np.ones(len(offsets), dtype=bool)
    delta = new.astype(np.int16) - old.astype(np.int16)

    if offset_from is not None:
        mask &= offsets >= offset_from
    if offset_to is not None:
        mask &= offsets <= offset_to
    if old_value is not None:
        mask &= old == old_value
    if new_value is not None:
        mask &= new == new_value

    if change_type == CHANGE_UP:
        mask &= delta > 0
    elif change_type == CHANGE_DOWN:
        mask &= delta < 0
    elif change_type == CHANGE_TO_ZERO:
        mask &= new == 0
    elif change_type == CHANGE_FROM_ZERO:
        mask &= old == 0
    elif change_type == CHANGE_DELTA and delta_value is not None:
        mask &= delta == delta_value

    return mask


def offset_width(snapshot_size):
    """Number of hex digits needed to show the largest offset (at least 4)."""
    return max(4, len(f"{max(snapshot_size - 1, 0):X}"))


def format_txt_line(offset, old, new, width):
    """Same format as the original command-line tool."""
    return f"Offset: {offset:0{width}X} | Old: {old:02X} ({old:3d}) -> New: {new:02X} ({new:3d})"


def write_txt(path, offsets, old, new, width, file1="", file2=""):
    with open(path, "w", encoding="utf-8") as out:
        if file1 or file2:
            out.write(f"Snapshot 1: {file1}\n")
            out.write(f"Snapshot 2: {file2}\n")
        out.write(f"Total differences: {len(offsets)}\n\n")
        for o, a, b in zip(offsets.tolist(), old.tolist(), new.tolist()):
            out.write(format_txt_line(o, a, b, width) + "\n")


def write_csv(path, offsets, old, new, width):
    with open(path, "w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(["offset_hex", "offset_dec", "old_hex", "old_dec", "new_hex", "new_dec", "delta"])
        for o, a, b in zip(offsets.tolist(), old.tolist(), new.tolist()):
            writer.writerow([f"{o:0{width}X}", o, f"{a:02X}", a, f"{b:02X}", b, b - a])


# =====================================================================
# GUI
# =====================================================================

class MemoryDiffApp:
    COLUMNS = [
        # id, heading, width, anchor
        ("offset", "Offset", 110, tk.W),
        ("old_hex", "Old (hex)", 80, tk.CENTER),
        ("old_dec", "Old (dec)", 80, tk.CENTER),
        ("new_hex", "New (hex)", 80, tk.CENTER),
        ("new_dec", "New (dec)", 80, tk.CENTER),
        ("delta", "Delta", 80, tk.CENTER),
    ]

    def __init__(self, root):
        self.root = root
        self.root.title("DOSBox Memory Diff")
        self.root.geometry("820x720")
        self.root.minsize(700, 500)

        # Full diff result
        self.offsets = np.array([], dtype=np.int64)
        self.old = np.array([], dtype=np.uint8)
        self.new = np.array([], dtype=np.uint8)
        self.width = 4
        self.compared_files = ("", "")

        # Indices into the full result, after filtering and sorting
        self.view = np.array([], dtype=np.int64)
        self.sort_column = "offset"
        self.sort_descending = False

        self.setup_style()
        self.setup_ui()

    # ----- Layout -----

    def setup_style(self):
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview", font=("Consolas", 10), rowheight=22)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"), padding=(16, 6))

    def setup_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # --- Snapshot files ---
        files = ttk.LabelFrame(main, text="Snapshots", padding=8)
        files.pack(fill=tk.X)
        files.columnconfigure(1, weight=1)

        self.var_file1 = tk.StringVar(value=DEFAULT_SNAP1)
        self.var_file2 = tk.StringVar(value=DEFAULT_SNAP2)

        for row, (label, var) in enumerate([("Before (snap 1):", self.var_file1),
                                            ("After (snap 2):", self.var_file2)]):
            ttk.Label(files, text=label, width=16).grid(row=row, column=0, sticky=tk.W, pady=2)
            ttk.Entry(files, textvariable=var).grid(row=row, column=1, sticky=tk.EW, padx=5, pady=2)
            ttk.Button(files, text="Browse...", command=lambda v=var: self.browse_file(v)).grid(
                row=row, column=2, pady=2)

        bottom_row = ttk.Frame(files)
        bottom_row.grid(row=2, column=0, columnspan=3, sticky=tk.EW, pady=(6, 0))

        self.var_autosave = tk.BooleanVar(value=True)
        ttk.Checkbutton(bottom_row, text=f"Auto-save results to {os.path.basename(AUTO_SAVE_FILE)}",
                        variable=self.var_autosave).pack(side=tk.LEFT)
        ttk.Button(bottom_row, text="Compare", style="Accent.TButton",
                   command=self.run_compare).pack(side=tk.RIGHT)

        # --- Filters ---
        filters = ttk.LabelFrame(main, text="Filters", padding=8)
        filters.pack(fill=tk.X, pady=8)

        self.var_from = tk.StringVar()
        self.var_to = tk.StringVar()
        self.var_old = tk.StringVar()
        self.var_new = tk.StringVar()
        self.var_change = tk.StringVar(value=CHANGE_ANY)
        self.var_delta = tk.StringVar()

        def entry(parent, var, width=10):
            e = ttk.Entry(parent, textvariable=var, width=width)
            e.bind("<Return>", lambda _e: self.apply_filters())
            return e

        ttk.Label(filters, text="Offset from (hex):").grid(row=0, column=0, sticky=tk.W)
        entry(filters, self.var_from).grid(row=0, column=1, padx=5, pady=2)
        ttk.Label(filters, text="to:").grid(row=0, column=2, sticky=tk.W)
        entry(filters, self.var_to).grid(row=0, column=3, padx=5, pady=2)

        ttk.Label(filters, text="Old value:").grid(row=1, column=0, sticky=tk.W)
        entry(filters, self.var_old).grid(row=1, column=1, padx=5, pady=2)
        ttk.Label(filters, text="New value:").grid(row=1, column=2, sticky=tk.W)
        entry(filters, self.var_new).grid(row=1, column=3, padx=5, pady=2)

        ttk.Label(filters, text="Change:").grid(row=2, column=0, sticky=tk.W)
        combo = ttk.Combobox(filters, textvariable=self.var_change, values=CHANGE_TYPES,
                             state="readonly", width=14)
        combo.grid(row=2, column=1, padx=5, pady=2)
        combo.bind("<<ComboboxSelected>>", lambda _e: self.apply_filters())
        ttk.Label(filters, text="Delta:").grid(row=2, column=2, sticky=tk.W)
        entry(filters, self.var_delta).grid(row=2, column=3, padx=5, pady=2)

        ttk.Label(filters, text="Values: 53 = decimal, 0x35 or 35h = hex, delta can be negative (-1)",
                  foreground="gray").grid(row=3, column=0, columnspan=4, sticky=tk.W, pady=(4, 0))

        btns = ttk.Frame(filters)
        btns.grid(row=0, column=4, rowspan=3, padx=(20, 0), sticky=tk.N)
        ttk.Button(btns, text="Apply", command=self.apply_filters).pack(fill=tk.X, pady=2)
        ttk.Button(btns, text="Reset", command=self.reset_filters).pack(fill=tk.X, pady=2)

        # --- Results table ---
        table = ttk.Frame(main)
        table.pack(fill=tk.BOTH, expand=True)

        self.tree = ttk.Treeview(table, columns=[c[0] for c in self.COLUMNS],
                                 show="headings", selectmode="extended")
        for col_id, heading, width, anchor in self.COLUMNS:
            self.tree.heading(col_id, text=heading, command=lambda c=col_id: self.sort_by(c))
            self.tree.column(col_id, width=width, anchor=anchor, stretch=True)

        self.tree.tag_configure("even", background="#f4f6f8")
        self.tree.tag_configure("up", foreground="#1b7a3a")
        self.tree.tag_configure("down", foreground="#b42318")

        vsb = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<Double-1>", lambda _e: self.copy_offsets())
        self.tree.bind("<Control-c>", lambda _e: self.copy_offsets())
        self.tree.bind("<Button-3>", self.show_context_menu)

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Copy offset(s)", command=self.copy_offsets)
        self.menu.add_command(label="Copy row(s)", command=self.copy_rows)

        # --- Status bar and export ---
        bar = ttk.Frame(main)
        bar.pack(fill=tk.X, pady=(8, 0))

        self.lbl_status = ttk.Label(bar, text="Choose two snapshots and press Compare.")
        self.lbl_status.pack(side=tk.LEFT)

        ttk.Button(bar, text="Export CSV", command=lambda: self.export("csv")).pack(side=tk.RIGHT, padx=2)
        ttk.Button(bar, text="Export TXT", command=lambda: self.export("txt")).pack(side=tk.RIGHT, padx=2)

    # ----- Actions -----

    def browse_file(self, var):
        start_dir = os.path.dirname(var.get()) if var.get() else BASE_DIR
        path = filedialog.askopenfilename(
            title="Select snapshot",
            initialdir=start_dir if os.path.isdir(start_dir) else BASE_DIR,
            filetypes=[("Binary snapshots", "*.bin"), ("All files", "*.*")],
        )
        if path:
            var.set(path)

    def run_compare(self):
        file1 = self.var_file1.get().strip()
        file2 = self.var_file2.get().strip()

        missing = [f for f in (file1, file2) if not os.path.isfile(f)]
        if missing:
            messagebox.showerror("File not found", "Could not find:\n" + "\n".join(missing))
            return

        try:
            data1 = load_snapshot(file1)
            data2 = load_snapshot(file2)
        except OSError as e:
            messagebox.showerror("Read error", str(e))
            return

        self.offsets, self.old, self.new = compute_diff(data1, data2)
        self.width = offset_width(min(len(data1), len(data2)))
        self.compared_files = (file1, file2)

        if len(data1) != len(data2):
            messagebox.showwarning(
                "Different sizes",
                f"Snapshot sizes differ ({len(data1):,} vs {len(data2):,} bytes).\n"
                "Only the overlapping part was compared.",
            )

        if self.var_autosave.get():
            try:
                write_txt(AUTO_SAVE_FILE, self.offsets, self.old, self.new, self.width, file1, file2)
            except OSError as e:
                messagebox.showwarning("Auto-save failed", str(e))

        self.apply_filters()

    def read_filters(self):
        """Reads all filter fields. Raises ValueError with a readable message on bad input."""
        def read(var, parser, name):
            try:
                return parser(var.get())
            except ValueError:
                raise ValueError(f"Invalid {name}: '{var.get()}'")

        return dict(
            offset_from=read(self.var_from, parse_offset, "offset (from)"),
            offset_to=read(self.var_to, parse_offset, "offset (to)"),
            old_value=read(self.var_old, parse_value, "old value"),
            new_value=read(self.var_new, parse_value, "new value"),
            change_type=self.var_change.get(),
            delta_value=read(self.var_delta, parse_value, "delta"),
        )

    def apply_filters(self):
        try:
            filters = self.read_filters()
        except ValueError as e:
            messagebox.showerror("Filter error", str(e))
            return

        mask = build_filter_mask(self.offsets, self.old, self.new, **filters)
        self.view = np.nonzero(mask)[0]
        self.apply_sort()
        self.refresh_table()

    def reset_filters(self):
        for var in (self.var_from, self.var_to, self.var_old, self.var_new, self.var_delta):
            var.set("")
        self.var_change.set(CHANGE_ANY)
        self.apply_filters()

    def sort_by(self, column):
        if self.sort_column == column:
            self.sort_descending = not self.sort_descending
        else:
            self.sort_column = column
            self.sort_descending = False
        self.apply_sort()
        self.refresh_table()

    def apply_sort(self):
        if len(self.view) == 0:
            return
        keys = {
            "offset": self.offsets,
            "old_hex": self.old, "old_dec": self.old,
            "new_hex": self.new, "new_dec": self.new,
            "delta": self.new.astype(np.int16) - self.old.astype(np.int16),
        }[self.sort_column][self.view]
        order = np.argsort(keys, kind="stable")
        if self.sort_descending:
            order = order[::-1]
        self.view = self.view[order]

        for col_id, heading, _, _ in self.COLUMNS:
            arrow = (" \u25bc" if self.sort_descending else " \u25b2") if col_id == self.sort_column else ""
            self.tree.heading(col_id, text=heading + arrow)

    def refresh_table(self):
        self.tree.delete(*self.tree.get_children())

        shown = self.view[:MAX_ROWS_DISPLAYED]
        for row_num, idx in enumerate(shown.tolist()):
            o, a, b = int(self.offsets[idx]), int(self.old[idx]), int(self.new[idx])
            delta = b - a
            tags = ["even"] if row_num % 2 == 0 else []
            tags.append("up" if delta > 0 else "down")
            self.tree.insert(
                "", tk.END, iid=str(idx), tags=tags,
                values=(f"0x{o:0{self.width}X}", f"{a:02X}", a, f"{b:02X}", b, f"{delta:+d}"),
            )

        total, matched = len(self.offsets), len(self.view)
        text = f"{total:,} differences  |  {matched:,} match filters"
        if matched > MAX_ROWS_DISPLAYED:
            text += f"  |  showing first {MAX_ROWS_DISPLAYED:,} (exports include all)"
        self.lbl_status.config(text=text)

    def selected_indices(self):
        return [int(iid) for iid in self.tree.selection()]

    def copy_to_clipboard(self, text, what):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.lbl_status.config(text=f"Copied {what} to clipboard.")

    def copy_offsets(self):
        idx = self.selected_indices()
        if idx:
            text = "\n".join(f"0x{int(self.offsets[i]):0{self.width}X}" for i in idx)
            self.copy_to_clipboard(text, f"{len(idx)} offset(s)")

    def copy_rows(self):
        idx = self.selected_indices()
        if idx:
            text = "\n".join(format_txt_line(int(self.offsets[i]), int(self.old[i]), int(self.new[i]), self.width)
                             for i in idx)
            self.copy_to_clipboard(text, f"{len(idx)} row(s)")

    def show_context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
        if self.tree.selection():
            self.menu.tk_popup(event.x_root, event.y_root)

    def export(self, kind):
        if len(self.view) == 0:
            messagebox.showinfo("Nothing to export", "There are no results matching the current filters.")
            return

        path = filedialog.asksaveasfilename(
            title="Export results",
            initialdir=BASE_DIR,
            initialfile=f"diff_results.{kind}",
            defaultextension=f".{kind}",
            filetypes=[("CSV files", "*.csv")] if kind == "csv" else [("Text files", "*.txt")],
        )
        if not path:
            return

        offsets, old, new = self.offsets[self.view], self.old[self.view], self.new[self.view]
        try:
            if kind == "csv":
                write_csv(path, offsets, old, new, self.width)
            else:
                write_txt(path, offsets, old, new, self.width, *self.compared_files)
        except OSError as e:
            messagebox.showerror("Export failed", str(e))
            return

        self.lbl_status.config(text=f"Exported {len(offsets):,} rows to {path}")


if __name__ == "__main__":
    root = tk.Tk()
    MemoryDiffApp(root)
    root.mainloop()