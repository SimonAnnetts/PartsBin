"""Pinout support for PartsBin.

A. Generated package diagram from pin data   (pure Python; Tk canvas + SVG export)
B. Capture of the manufacturer's pinout figure from a datasheet PDF (needs PyMuPDF)

PNG output needs the optional PyMuPDF package:  pip install pymupdf
"""
import math
import os
import re
import struct
import tempfile
import threading
import tkinter as tk
import urllib.parse
import urllib.request
from tkinter import ttk, messagebox

try:                                  # optional dependency
    import fitz                       # PyMuPDF
except ImportError:
    fitz = None

NEED_FITZ = "This needs PyMuPDF. Install it with:  pip install pymupdf"

CW = 7.0        # approximate character width at 12 px
FS = 12         # font size (px)
PITCH = 24      # pin pitch (px)
INK, FILL, GREY = "#222222", "#f4f4f0", "#777777"


# ------------------------------------------------------------------ A. diagram
def parse_pinout(text):
    """'1 CLK, 2 Q1, ...' -> [(1,'CLK'), ...]. Returns [] unless pins are exactly 1..N."""
    text = re.sub(r"\([^)]*[ ,][^)]*\)", "", text or "")     # drop "(front view, ...)" notes
    pins = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"(\d+)\s*(.*)$", part)
        if not m:
            return []
        pins.append((int(m[1]), m[2].strip()))
    nums = sorted(n for n, _ in pins)
    return pins if pins and nums == list(range(1, len(pins) + 1)) else []


def _clean(name):
    """KiCad overbar '~{X}' -> ('X', True)."""
    over = bool(re.fullmatch(r"~\{[^}]*\}", name))
    return re.sub(r"~\{([^}]*)\}", r"\1", name), over


def _text(x, y, s, anchor="c", size=FS, bold=False, over=False, color=INK):
    return dict(t="text", x=x, y=y, s=s, anchor=anchor, size=size, bold=bold,
                over=over, color=color)


def build_diagram(title, pins):
    """Return {'w','h','items'} describing a top-view package drawing, or None."""
    n = len(pins)
    if n < 2 or n > 40:
        return None
    names = {num: _clean(nm) for num, nm in pins}
    items = []
    if n % 2 == 0 and n >= 4:                               # dual in-line (DIP/SOIC)
        rows = n // 2
        lw = max(len(names[i + 1][0]) for i in range(rows))
        rw = max(len(names[n - i][0]) for i in range(rows))
        bw = max(110, int((lw + rw) * CW) + 44)
        stub, pad = 16, 52
        bx, by = pad, 46
        bh = 22 + (rows - 1) * PITCH + 16
        w, h = bw + 2 * pad, by + bh + 14
        items.append(_text(w / 2, 16, title, bold=True, size=14))
        items.append(dict(t="rect", x=bx, y=by, w=bw, h=bh, fill=FILL))
        items.append(dict(t="notch", x=bx + bw / 2, y=by, r=7))
        for i in range(rows):
            y = by + 22 + i * PITCH
            left, right = i + 1, n - i
            for num, side in ((left, "L"), (right, "R")):
                nm, over = names[num]
                if side == "L":
                    items.append(dict(t="line", x1=bx - stub, y1=y, x2=bx, y2=y))
                    items.append(_text(bx - stub - 4, y, str(num), "e", 11, color=GREY))
                    items.append(_text(bx + 6, y, nm, "w", over=over))
                else:
                    items.append(dict(t="line", x1=bx + bw, y1=y, x2=bx + bw + stub, y2=y))
                    items.append(_text(bx + bw + stub + 4, y, str(num), "w", 11, color=GREY))
                    items.append(_text(bx + bw - 6, y, nm, "e", over=over))
        items.append(dict(t="dot", x=bx + 11, y=by + 9, r=3))          # pin-1 marker
        return dict(w=w, h=h, items=items)
    # in-line view for 3-pin parts and odd pin counts (generic, not package-specific)
    if n > 10:
        return None
    colw = max(44, int(max(len(names[i][0]) for i in names) * CW) + 12)
    by, bh, stub = 46, 30, 18
    bw = colw * n
    w = max(bw + 48, 190)
    bx = (w - bw) / 2
    h = by + bh + stub + 34
    items.append(_text(w / 2, 16, title, bold=True, size=14))
    items.append(dict(t="rect", x=bx, y=by, w=bw, h=bh, fill=FILL))
    for i in range(n):
        cx = bx + colw * i + colw / 2
        nm, over = names[i + 1]
        items.append(_text(cx, by + bh / 2, nm, "c", over=over))
        items.append(dict(t="line", x1=cx, y1=by + bh, x2=cx, y2=by + bh + stub))
        items.append(_text(cx, by + bh + stub + 10, str(i + 1), "c", 11, color=GREY))
    items.append(_text(w / 2, h - 8, "generic in-line view (see datasheet)",
                       "c", 9, color=GREY))
    return dict(w=w, h=max(h, by + bh + stub + 34), items=items)


def draw_on_canvas(canvas, diagram, scale):
    """Draw a diagram on a tk.Canvas (white background)."""
    s = scale
    for it in diagram["items"]:
        t = it["t"]
        if t == "rect":
            canvas.create_rectangle(it["x"] * s, it["y"] * s, (it["x"] + it["w"]) * s,
                                    (it["y"] + it["h"]) * s, fill=it["fill"], outline=INK,
                                    width=max(1, round(1.5 * s)))
        elif t == "line":
            canvas.create_line(it["x1"] * s, it["y1"] * s, it["x2"] * s, it["y2"] * s,
                               fill=INK, width=max(1, round(1.5 * s)))
        elif t == "notch":
            r = it["r"] * s
            canvas.create_arc(it["x"] * s - r, it["y"] * s - r, it["x"] * s + r, it["y"] * s + r,
                              start=180, extent=180, style="arc", outline=INK,
                              width=max(1, round(1.5 * s)))
        elif t == "dot":
            r = it["r"] * s
            canvas.create_oval(it["x"] * s - r, it["y"] * s - r, it["x"] * s + r,
                               it["y"] * s + r, fill=INK, outline=INK)
        elif t == "text":
            anchor = {"c": "center"}.get(it["anchor"], it["anchor"])
            px = max(6, int(it["size"] * s))
            font = ("Helvetica", -px, "bold" if it["bold"] else "normal")
            canvas.create_text(it["x"] * s, it["y"] * s, text=it["s"], anchor=anchor,
                               font=font, fill=it["color"])
            if it["over"]:
                _overline(canvas, it, s)


def _overline(canvas, it, s):
    wd = len(it["s"]) * CW * it["size"] / FS
    x = it["x"] - {"c": wd / 2, "w": 0, "e": wd}[it["anchor"]]
    y = it["y"] - it["size"] * 0.62
    canvas.create_line(x * s, y * s, (x + wd) * s, y * s, fill=it["color"])


def diagram_svg(diagram):
    """SVG text for a diagram (also the source for PNG export)."""
    from xml.sax.saxutils import escape
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{diagram["w"]}" '
           f'height="{diagram["h"]}" viewBox="0 0 {diagram["w"]} {diagram["h"]}">',
           f'<rect width="100%" height="100%" fill="white"/>']
    for it in diagram["items"]:
        t = it["t"]
        if t == "rect":
            out.append(f'<rect x="{it["x"]}" y="{it["y"]}" width="{it["w"]}" height="{it["h"]}" '
                       f'fill="{it["fill"]}" stroke="{INK}" stroke-width="1.5"/>')
        elif t == "line":
            out.append(f'<line x1="{it["x1"]}" y1="{it["y1"]}" x2="{it["x2"]}" y2="{it["y2"]}" '
                       f'stroke="{INK}" stroke-width="1.5"/>')
        elif t == "notch":
            x, y, r = it["x"], it["y"], it["r"]
            out.append(f'<path d="M {x - r} {y} A {r} {r} 0 0 0 {x + r} {y}" fill="none" '
                       f'stroke="{INK}" stroke-width="1.5"/>')
        elif t == "dot":
            out.append(f'<circle cx="{it["x"]}" cy="{it["y"]}" r="{it["r"]}" fill="{INK}"/>')
        elif t == "text":
            anc = {"c": "middle", "w": "start", "e": "end"}[it["anchor"]]
            weight = "bold" if it["bold"] else "normal"
            out.append(f'<text x="{it["x"]}" y="{it["y"] + it["size"] * 0.35:.1f}" '
                       f'text-anchor="{anc}" font-family="Helvetica, Arial, sans-serif" '
                       f'font-size="{it["size"]}" font-weight="{weight}" fill="{it["color"]}">'
                       f'{escape(it["s"])}</text>')
            if it["over"]:
                wd = len(it["s"]) * CW * it["size"] / FS
                x = it["x"] - {"c": wd / 2, "w": 0, "e": wd}[it["anchor"]]
                y = it["y"] - it["size"] * 0.62
                out.append(f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x + wd:.1f}" y2="{y:.1f}" '
                           f'stroke="{it["color"]}" stroke-width="1"/>')
    out.append("</svg>")
    return "\n".join(out)


def diagram_png(diagram, zoom=3):
    """PNG bytes of a diagram (needs PyMuPDF)."""
    if fitz is None:
        raise RuntimeError(NEED_FITZ)
    doc = fitz.open(stream=diagram_svg(diagram).encode("utf-8"), filetype="svg")
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return pix.tobytes("png")


# ----------------------------------------------------------- stored PNG display
def png_size(data):
    return struct.unpack(">II", data[16:24])


def fit_png(data, max_w, max_h):
    """Downscale PNG bytes to fit the box (smooth with PyMuPDF, else nearest-neighbour)."""
    w, h = png_size(data)
    f = max(w / max(max_w, 1), h / max(max_h, 1), 1)
    if f <= 1:
        return data
    if fitz is not None:
        try:   # open the PNG as a one-page document and render it smaller: smooth downscale
            doc = fitz.open(stream=data, filetype="png")   # keep doc alive: page needs it
            page = doc[0]
            m = fitz.Matrix(w / f / page.rect.width, h / f / page.rect.height)
            return page.get_pixmap(matrix=m, alpha=False).tobytes("png")
        except Exception:
            pass
    return data    # caller falls back to PhotoImage.subsample


# ------------------------------------------------------------ B. datasheet figure
_PIN_PATTERNS = [   # (regex, weight): headings that usually sit next to the pinout figure
    (r"pin[\s\-]*(configurations?|assignments?|diagrams?|connections?)", 3),
    (r"connection\s+diagrams?|terminal\s+(assignments?|connections?)|pinning|pin[\s\-]?out", 3),
    (r"pin\s+(descriptions?|functions?)", 1),
]
_HEADINGS = ("pin configuration", "pin assignment", "connection diagram", "pinning",
             "pinout", "pin diagram")


def local_path(ref):
    """A stored datasheet reference as a filesystem path: accepts plain paths, ~ and file:// URLs."""
    ref = (ref or "").strip().strip('"')
    if ref.lower().startswith("file://"):
        ref = urllib.request.url2pathname(urllib.parse.urlparse(ref).path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:", ref):
            ref = ref[1:]
    return os.path.expanduser(ref)


def download_pdf(url, dest, max_mb=40):
    """Download a datasheet. Accepts http(s) URLs or a local file path."""
    if os.path.isfile(local_path(url)):
        return local_path(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) PartsBin/1.0", "Accept": "application/pdf,*/*"})
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
        size = 0
        while chunk := r.read(1 << 18):
            size += len(chunk)
            if size > max_mb << 20:
                raise ValueError(f"Datasheet larger than {max_mb} MB")
            f.write(chunk)
    with open(dest, "rb") as f:
        if f.read(5) != b"%PDF-":
            raise ValueError("The link did not return a PDF (it may need a browser/login)")
    return dest


def find_pin_pages(doc, scan=40):
    """Page numbers most likely to hold the pinout figure, best first.
    Scanned datasheets have no text; then the first few pages are suggested."""
    scored = []
    for pno in range(min(len(doc), scan)):
        text = re.sub(r"\s+", " ", doc[pno].get_text())
        score = sum(wt * len(re.findall(rx, text, re.I)) for rx, wt in _PIN_PATTERNS)
        if score:
            scored.append((-score, pno))
    return [p for _, p in sorted(scored)][:6] or list(range(min(len(doc), 4)))


class CaptureDialog(tk.Toplevel):
    """Show datasheet pages; the user drags a box round the pinout; PNG bytes are returned."""

    def __init__(self, parent, pdf_path, title, on_done):
        super().__init__(parent)
        self.title(f"Capture pinout - {title}")
        self.geometry("920x760")
        self.transient(parent)
        self.doc = fitz.open(pdf_path)
        self.on_done = on_done
        self.pages = find_pin_pages(self.doc)
        self.pno, self.zoom = self.pages[0], 1.0
        self.sel = None
        self._photo = None

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=6, pady=4)
        ttk.Button(bar, text="< Prev", command=lambda: self.goto(self.pno - 1)).pack(side="left")
        ttk.Button(bar, text="Next >", command=lambda: self.goto(self.pno + 1)).pack(side="left")
        self.lbl = ttk.Label(bar, width=14)
        self.lbl.pack(side="left", padx=6)
        ttk.Label(bar, text="Suggested pages:").pack(side="left")
        for p in self.pages:
            ttk.Button(bar, text=str(p + 1), width=3,
                       command=lambda p=p: self.goto(p)).pack(side="left", padx=1)
        ttk.Button(bar, text="Use selection", command=self.accept).pack(side="right")
        ttk.Label(self, text="Drag a box around the pinout figure, then click 'Use selection'.",
                  foreground="gray").pack(anchor="w", padx=8)

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=6, pady=6)
        self.cv = tk.Canvas(frame, bg="#888888", highlightthickness=0, cursor="crosshair")
        sb = ttk.Scrollbar(frame, command=self.cv.yview)
        self.cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.cv.pack(fill="both", expand=True)
        self.cv.bind("<ButtonPress-1>", self.press)
        self.cv.bind("<B1-Motion>", self.drag)
        self.cv.bind("<MouseWheel>", lambda e: self.cv.yview_scroll(-1 * (e.delta // 120), "units"))
        self.cv.bind("<Button-4>", lambda e: self.cv.yview_scroll(-3, "units"))
        self.cv.bind("<Button-5>", lambda e: self.cv.yview_scroll(3, "units"))
        self.after(50, lambda: self.goto(self.pno))

    def goto(self, pno):
        if not 0 <= pno < len(self.doc):
            return
        self.pno = pno
        page = self.doc[pno]
        width = max(self.cv.winfo_width() - 4, 600)
        self.zoom = min(width / page.rect.width, 2.0)
        pix = page.get_pixmap(matrix=fitz.Matrix(self.zoom, self.zoom), alpha=False)
        self._photo = tk.PhotoImage(data=pix.tobytes("png"))
        self.cv.delete("all")
        self.cv.create_image(0, 0, image=self._photo, anchor="nw")
        self.cv.configure(scrollregion=(0, 0, pix.width, pix.height))
        self.cv.yview_moveto(0)
        for kw in _HEADINGS:
            for r in page.search_for(kw)[:3]:       # outline where the headings are
                self.cv.create_rectangle(r.x0 * self.zoom, r.y0 * self.zoom, r.x1 * self.zoom,
                                         r.y1 * self.zoom, outline="#e03030", width=2)
        self.lbl.config(text=f"Page {pno + 1}/{len(self.doc)}")
        self.sel = None

    def press(self, e):
        self.x0, self.y0 = self.cv.canvasx(e.x), self.cv.canvasy(e.y)
        self.cv.delete("sel")
        self.sel = None

    def drag(self, e):
        x1, y1 = self.cv.canvasx(e.x), self.cv.canvasy(e.y)
        self.cv.delete("sel")
        self.cv.create_rectangle(self.x0, self.y0, x1, y1, outline="#2060ff", width=2, tags="sel")
        self.sel = (min(self.x0, x1), min(self.y0, y1), max(self.x0, x1), max(self.y0, y1))

    def accept(self):
        if not self.sel or self.sel[2] - self.sel[0] < 10 or self.sel[3] - self.sel[1] < 10:
            return messagebox.showinfo("Capture", "Drag a box around the pinout first.", parent=self)
        z = self.zoom
        clip = fitz.Rect(*(v / z for v in self.sel)) & self.doc[self.pno].rect
        pix = self.doc[self.pno].get_pixmap(matrix=fitz.Matrix(3, 3), clip=clip, alpha=False)
        png = pix.tobytes("png")
        self.destroy()
        self.on_done(png)


def capture_from_datasheet(parent, url, title, on_done, status=lambda m: None):
    """Download in a background thread, then open the capture dialog."""
    if fitz is None:
        return messagebox.showinfo("Capture from datasheet", NEED_FITZ, parent=parent)
    if not url:
        return messagebox.showinfo("Capture from datasheet",
                                   "This part has no datasheet link (or local PDF path).",
                                   parent=parent)
    status("Downloading datasheet...")
    tmp = tempfile.mkdtemp(prefix="partsbin_")
    dest = os.path.join(tmp, "datasheet.pdf")

    def work():
        try:
            path = download_pdf(url, dest)
        except Exception as e:
            parent.after(0, lambda: (status(f"Datasheet download failed: {e}"),
                                     messagebox.showerror("Datasheet", f"{e}", parent=parent)))
            return

        def show():
            status("Datasheet loaded.")
            try:
                CaptureDialog(parent, path, title, on_done)
            except Exception as e:
                messagebox.showerror("Datasheet", f"Could not open the PDF: {e}", parent=parent)
        parent.after(0, show)
    threading.Thread(target=work, daemon=True).start()
