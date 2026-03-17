import os

for _var in ("TCL_LIBRARY", "TK_LIBRARY"):
    os.environ.pop(_var, None)

import base64
import io
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

from PIL import Image, ImageTk
import requests

sys.path.insert(0, str(Path(__file__).parent))
from image_processor import ImageProcessor

API_BASE = "http://localhost:5001"
PREVIEW_MAX = 512


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("TransferMaker")
        self.root.minsize(780, 580)

        # State shared across steps
        self.source_image_path: str | None = None
        self.processed_b64: str | None = None
        self.processed_pil: Image.Image | None = None
        self.settings: dict = {}
        self.generated_images: list[Image.Image] = []
        self.selected_image: Image.Image | None = None
        self.clamped_image: Image.Image | None = None
        self.svg_string: str | None = None

        self._photo_refs: list = []   # prevent GC of PhotoImages
        self._step_frame: ttk.Frame | None = None

        self.container = ttk.Frame(self.root, padding=16)
        self.container.pack(fill=tk.BOTH, expand=True)

        # Persistent toolbar at the bottom
        toolbar = ttk.Frame(self.root, padding=(16, 0, 16, 8))
        toolbar.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Separator(self.root, orient="horizontal").pack(fill=tk.X, side=tk.BOTTOM)
        def _confirm_reset():
            if messagebox.askyesno("Start Over",
                                   "Discard the current session and start over?"):
                self._reset_session()

        ttk.Button(toolbar, text="Start Over",
                   command=_confirm_reset).pack(side=tk.RIGHT)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.step1_wait_for_api()

    def _on_close(self):
        self.root.destroy()
        os._exit(0)

    def _reset_session(self):
        """Clear all session state and return to the upload step."""
        self.source_image_path = None
        self.processed_b64 = None
        self.processed_pil = None
        self.settings = {}
        self.generated_images = []
        self.selected_image = None
        self.clamped_image = None
        self.svg_string = None
        self.step2_upload_image()

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _new_frame(self) -> ttk.Frame:
        self._photo_refs.clear()
        if self._step_frame:
            self._step_frame.destroy()
        self._step_frame = ttk.Frame(self.container)
        self._step_frame.pack(fill=tk.BOTH, expand=True)
        return self._step_frame

    def _header(self, parent, title: str, subtitle: str = ""):
        ttk.Label(parent, text=title, font=("", 14, "bold")).pack(anchor="w")
        if subtitle:
            ttk.Label(parent, text=subtitle, foreground="gray").pack(anchor="w", pady=(2, 0))
        ttk.Separator(parent, orient="horizontal").pack(fill=tk.X, pady=(8, 12))

    def _photo(self, pil_img: Image.Image, max_size: int = PREVIEW_MAX) -> ImageTk.PhotoImage:
        thumb = pil_img.copy()
        thumb.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        ph = ImageTk.PhotoImage(thumb)
        self._photo_refs.append(ph)
        return ph

    def _nav_row(self, parent, back_cmd=None, fwd_text="Next →",
                 fwd_cmd=None, fwd_state="normal") -> ttk.Button:
        row = ttk.Frame(parent)
        row.pack(side=tk.BOTTOM, anchor="w", pady=12)
        if back_cmd:
            ttk.Button(row, text="← Back", command=back_cmd).pack(side=tk.LEFT, padx=(0, 6))
        btn = ttk.Button(row, text=fwd_text, command=fwd_cmd, state=fwd_state)
        btn.pack(side=tk.LEFT)
        return btn

    # ── Step 1: Wait for API ───────────────────────────────────────────────────

    def step1_wait_for_api(self):
        frame = self._new_frame()
        self._header(frame, "TransferMaker",
                     "Waiting for KoboldCpp to finish loading the model…")

        status_var = tk.StringVar(value="Connecting…")
        ttk.Label(frame, textvariable=status_var).pack(pady=6)
        bar = ttk.Progressbar(frame, mode="indeterminate", length=380)
        bar.pack()
        bar.start(12)

        def poll():
            import time
            while True:
                try:
                    r = requests.get(f"{API_BASE}/sdapi/v1/options", timeout=3)
                    if r.status_code == 200:
                        self.root.after(0, self.step2_upload_image)
                        return
                except Exception:
                    pass
                self.root.after(0, lambda: status_var.set("Waiting for model to load…"))
                time.sleep(2)

        threading.Thread(target=poll, daemon=True).start()

    # ── Step 2: Upload image ───────────────────────────────────────────────────

    def step2_upload_image(self):
        frame = self._new_frame()
        self._header(frame, "Step 1 of 8 — Upload Image",
                     "Choose a reference photo or artwork to convert into a vinyl transfer design.")

        preview_lbl = ttk.Label(frame)
        preview_lbl.pack(pady=6)
        info_var = tk.StringVar(value="No image selected.")
        ttk.Label(frame, textvariable=info_var, foreground="gray").pack()

        next_btn = self._nav_row(frame, fwd_text="Next →", fwd_state="disabled")

        def choose():
            path = filedialog.askopenfilename(
                title="Select reference image",
                filetypes=[
                    ("Images", "*.png *.jpg *.jpeg *.webp *.heic *.heif *.tiff *.tif *.gif"),
                    ("All files", "*.*"),
                ],
            )
            if not path:
                return
            try:
                processor = ImageProcessor(max_dimension=1024)
                b64, _ = processor.process_image(path)
                if not b64:
                    messagebox.showerror("Error", "Unsupported image format.")
                    return
                self.source_image_path = path
                self.processed_b64 = b64
                img_bytes = base64.b64decode(b64)
                pil = Image.open(io.BytesIO(img_bytes))
                pil.load()
                self.processed_pil = pil

                ph = self._photo(pil)
                preview_lbl.configure(image=ph)
                info_var.set(f"{Path(path).name}  —  {pil.width} × {pil.height} px")
                next_btn.configure(state="normal", command=self.step3_confirm_image)
            except Exception as e:
                messagebox.showerror("Error", str(e))

        ttk.Button(frame, text="Choose Image…", command=choose).pack(pady=8)

    # ── Step 3: Confirm image ──────────────────────────────────────────────────

    def step3_confirm_image(self):
        frame = self._new_frame()
        self._header(frame, "Step 2 of 8 — Confirm Image",
                     "Confirm to continue.")

        ph = self._photo(self.processed_pil)
        ttk.Label(frame, image=ph).pack(pady=6)
        ttk.Label(frame,
                  text=f"{self.processed_pil.width} × {self.processed_pil.height} px",
                  foreground="gray").pack()

        self._nav_row(frame, back_cmd=self.step2_upload_image,
                      fwd_text="Confirm →", fwd_cmd=self.step4_settings)

    # ── Step 4: Settings ───────────────────────────────────────────────────────

    def step4_settings(self):
        frame = self._new_frame()
        self._header(frame, "Step 3 of 8 — Generation Settings")

        form = ttk.Frame(frame, padding=(40, 0))
        form.pack(fill=tk.X)

        def field(label, default, cls=ttk.Entry, **kw):
            r = ttk.Frame(form)
            r.pack(fill=tk.X, pady=3)
            ttk.Label(r, text=label, width=32, anchor="e").pack(side=tk.LEFT, padx=(0, 8))
            var = tk.StringVar(value=str(default))
            cls(r, textvariable=var, **kw).pack(side=tk.LEFT)
            return var

        width_var    = field("Output width (mm):",           200)
        height_var   = field("Output height (mm):",          200)
        thick_var    = field("Min feature thickness (mm):",  1.5)
        attempts_var = field("Generation attempts (1–8):",   4, ttk.Spinbox, from_=1, to=8, width=5)

        # ── Dynamic color list ─────────────────────────────────────────────────
        color_section = ttk.LabelFrame(form, text="Colors  (name or hex, e.g. 'black', '#c0392b')")
        color_section.pack(fill=tk.X, pady=(10, 0))

        color_rows: list[tuple[tk.StringVar, tk.Frame]] = []

        def _refresh_swatch(swatch: tk.Label, var: tk.StringVar, *_):
            from PIL import ImageColor
            try:
                r, g, b = ImageColor.getrgb(var.get().strip())[:3]
                swatch.configure(bg=f"#{r:02x}{g:02x}{b:02x}")
            except Exception:
                swatch.configure(bg="#ffffff")

        def add_color_row(name: str = ""):
            if len(color_rows) >= 8:
                return
            row = ttk.Frame(color_section)
            row.pack(fill=tk.X, padx=4, pady=2)
            var = tk.StringVar(value=name)
            ttk.Entry(row, textvariable=var, width=22).pack(side=tk.LEFT)
            swatch = tk.Label(row, width=3, bg="#ffffff", relief="sunken")
            swatch.pack(side=tk.LEFT, padx=4)
            var.trace_add("write", lambda *_: _refresh_swatch(swatch, var))
            _refresh_swatch(swatch, var)

            def remove(r=row, v=var):
                if len(color_rows) <= 1:
                    return
                r.destroy()
                color_rows.remove(next(x for x in color_rows if x[0] is v))

            ttk.Button(row, text="−", width=2, command=remove).pack(side=tk.LEFT)
            color_rows.append((var, row))

        for c in ("black", "white", "red"):
            add_color_row(c)

        ttk.Button(color_section, text="+ Add color",
                   command=add_color_row).pack(anchor="w", padx=4, pady=(0, 4))

        ttk.Label(form, text="Special requests (optional):").pack(anchor="w", pady=(10, 2))
        requests_box = tk.Text(form, height=3, wrap=tk.WORD, width=52)
        requests_box.pack(fill=tk.X)

        def go():
            try:
                w = float(width_var.get())
                h = float(height_var.get())
                t = float(thick_var.get())
                a = int(attempts_var.get())
                assert w > 0 and h > 0 and t > 0 and 1 <= a <= 8
            except Exception:
                messagebox.showerror("Invalid input", "Please check all values are valid.")
                return
            colors = [v.get().strip() for v, _ in color_rows if v.get().strip()]
            if not colors:
                messagebox.showerror("Invalid input", "Add at least one color.")
                return
            self.settings = {
                "width_mm": w, "height_mm": h, "min_thickness_mm": t,
                "colors": colors, "attempts": a,
                "special_requests": requests_box.get("1.0", tk.END).strip(),
            }
            self.step5_generate()

        self._nav_row(frame, back_cmd=self.step3_confirm_image,
                      fwd_text="Generate →", fwd_cmd=go)

    # ── Step 5: Generate ───────────────────────────────────────────────────────

    def step5_generate(self):
        frame = self._new_frame()
        attempts = self.settings["attempts"]
        self._header(frame, "Step 4 of 8 — Generating Designs",
                     f"Generating {attempts} variation(s). This may take a few minutes.")

        status_var = tk.StringVar(value="Starting…")
        ttk.Label(frame, textvariable=status_var).pack()
        bar = ttk.Progressbar(frame, length=440, maximum=attempts)
        bar.pack(pady=4)

        # Horizontally scrollable thumbnail strip
        canvas = tk.Canvas(frame, height=220, highlightthickness=0)
        hscroll = ttk.Scrollbar(frame, orient="horizontal", command=canvas.xview)
        canvas.configure(xscrollcommand=hscroll.set)
        canvas.pack(fill=tk.X, pady=6)
        hscroll.pack(fill=tk.X)
        thumb_row = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=thumb_row, anchor="nw")
        thumb_row.bind("<Configure>",
                       lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        self.generated_images = []
        colors = self.settings["colors"]
        color_list = ", ".join(colors[:-1]) + f" and {colors[-1]}" if len(colors) > 1 else colors[0]
        special = self.settings["special_requests"]
        prompt = (
            f"Convert the reference image into a bold high contrast graphic design "
            f"using only {color_list} suitable for vinyl cutting. "
            f"Flat solid shapes, no gradients, no fine detail, clean sharp edges, "
            f"white background, style of a screen print or sticker design."
        )
        if special:
            prompt += f" {special}"

        def generate_one() -> Image.Image:
            payload = {
                "prompt": prompt,
                "cfg_scale": 1,
                "steps": 6,
                "seed": -1,
                "sampler_name": "Euler",
                "width": self.processed_pil.width,
                "height": self.processed_pil.height,
                "extra_images": [self.processed_b64],
            }
            r = requests.post(f"{API_BASE}/sdapi/v1/img2img", json=payload, timeout=300)
            r.raise_for_status()
            img = Image.open(io.BytesIO(base64.b64decode(r.json()["images"][0])))
            img.load()
            return img

        def run():
            for i in range(attempts):
                self.root.after(0, lambda i=i: status_var.set(
                    f"Generating image {i + 1} of {attempts}…"))
                try:
                    img = generate_one()
                    self.generated_images.append(img)
                    idx = len(self.generated_images) - 1

                    def add_thumb(img=img, idx=idx):
                        ph = self._photo(img, max_size=180)
                        cell = ttk.Frame(thumb_row)
                        cell.grid(row=0, column=idx, padx=4)
                        ttk.Label(cell, image=ph).pack()
                        ttk.Label(cell, text=f"#{idx + 1}").pack()
                        bar.step(1)

                    self.root.after(0, add_thumb)
                except Exception as e:
                    err = str(e)
                    self.root.after(0, lambda err=err: status_var.set(f"Error: {err}"))

            self.root.after(0, done)

        def done():
            status_var.set("Done!" if self.generated_images else "No images were generated.")
            if self.generated_images:
                ttk.Button(frame, text="Pick an image →",
                           command=self.step6_pick_image).pack(pady=6)
            ttk.Button(frame, text="← Change settings",
                       command=self.step4_settings).pack()

        threading.Thread(target=run, daemon=True).start()

    # ── Step 6: Pick image ─────────────────────────────────────────────────────

    def step6_pick_image(self):
        frame = self._new_frame()
        self._header(frame, "Step 5 of 8 — Pick a Design",
                     "Click a design to select it, then click Continue.")

        selected_idx = tk.IntVar(value=-1)
        cells: list[ttk.Frame] = []

        cols = min(4, len(self.generated_images))
        grid_frame = ttk.Frame(frame)
        grid_frame.pack(pady=6)

        for idx, img in enumerate(self.generated_images):
            ph = self._photo(img, max_size=200)
            cell = ttk.Frame(grid_frame, relief="flat", borderwidth=2)
            cell.grid(row=idx // cols, column=idx % cols, padx=6, pady=6)
            cells.append(cell)

            lbl = tk.Label(cell, image=ph, cursor="hand2")
            lbl.image = ph
            lbl.pack()
            ttk.Label(cell, text=f"Option {idx + 1}").pack()

            def select(i=idx, c=cell):
                selected_idx.set(i)
                for c_ in cells:
                    c_.configure(relief="flat")
                c.configure(relief="solid")

            lbl.bind("<Button-1>", lambda e, i=idx, c=cell: select(i, c))

        def go():
            i = selected_idx.get()
            if i < 0:
                messagebox.showinfo("No selection", "Click an image to select it first.")
                return
            self.selected_image = self.generated_images[i]
            self.step7_clamp_colors()

        self._nav_row(frame, back_cmd=self.step5_generate,
                      fwd_text="Continue →", fwd_cmd=go)

    # ── Step 7: Clamp colors ───────────────────────────────────────────────────

    def step7_clamp_colors(self):
        frame = self._new_frame()
        colors = self.settings["colors"]
        self._header(frame, "Step 6 of 8 — Upscale & Color Clamping",
                     f"Upscaling then clamping to: {', '.join(colors)}")

        status_var = tk.StringVar(value="Upscaling…")
        ttk.Label(frame, textvariable=status_var).pack(pady=4)
        bar = ttk.Progressbar(frame, mode="indeterminate", length=380)
        bar.pack(pady=6)
        bar.start(12)

        def run():
            from PIL import ImageColor

            # ── Upscale selected image ─────────────────────────────────────────
            upscaled = self.selected_image
            try:
                buf = io.BytesIO()
                self.selected_image.save(buf, format="PNG")
                up = requests.post(
                    f"{API_BASE}/sdapi/v1/upscale",
                    json={"image": base64.b64encode(buf.getvalue()).decode(),
                          "upscaling_resize": 4},
                    timeout=300,
                )
                if up.status_code == 200:
                    upscaled = Image.open(io.BytesIO(base64.b64decode(up.json()["image"])))
                    upscaled.load()
            except Exception as e:
                self.root.after(0, lambda: status_var.set(f"Upscale failed ({e}), using original."))

            self.root.after(0, lambda: status_var.set("Clamping colors…"))

            # ── Build palette from named colors ────────────────────────────────
            # White is always included — the generated image has a white
            # background and omitting it causes background pixels to bleed
            # into the nearest non-white color.
            palette_rgb: list[tuple[int, int, int]] = [(255, 255, 255)]
            for name in colors:
                try:
                    rgb = ImageColor.getrgb(name.strip())[:3]
                    if rgb not in palette_rgb:
                        palette_rgb.append(rgb)
                except Exception:
                    pass

            if not palette_rgb:
                self.clamped_image = upscaled.convert("RGB")
                self.root.after(0, show)
                return

            # PIL quantize with a custom palette does nearest-neighbor mapping
            # (no dithering) to the exact colors we specify.
            palette_img = Image.new("P", (1, 1))
            flat = []
            for r, g, b in palette_rgb:
                flat.extend([r, g, b])
            flat.extend([0] * (768 - len(flat)))   # pad to 256 entries
            palette_img.putpalette(flat)

            img = upscaled.convert("RGB")
            self.clamped_image = img.quantize(palette=palette_img, dither=0).convert("RGB")
            self.root.after(0, show)

        def show():
            bar.stop()
            bar.destroy()
            pair = ttk.Frame(frame)
            pair.pack(pady=6)
            for label, src in [("Selected", self.selected_image),
                                ("Color Clamped", self.clamped_image)]:
                cell = ttk.Frame(pair)
                cell.pack(side=tk.LEFT, padx=16)
                ph = self._photo(src, max_size=300)
                ttk.Label(cell, image=ph).pack()
                ttk.Label(cell, text=label).pack()
            self._nav_row(frame, back_cmd=self.step6_pick_image,
                          fwd_text="Vectorize →", fwd_cmd=self.step8_vectorize)

        threading.Thread(target=run, daemon=True).start()

    # ── Step 8: Vectorize ──────────────────────────────────────────────────────

    def step8_vectorize(self):
        frame = self._new_frame()
        self._header(frame, "Step 7 of 8 — Vectorization",
                     "Tracing image into SVG paths for vinyl cutting.")

        bar = ttk.Progressbar(frame, mode="indeterminate", length=380)
        bar.pack(pady=10)
        bar.start(12)
        status_var = tk.StringVar(value="Tracing…")
        ttk.Label(frame, textvariable=status_var).pack()

        def run():
            try:
                import vtracer
                buf = io.BytesIO()
                self.clamped_image.convert("RGBA").save(buf, format="PNG")
                self.svg_string = vtracer.convert_raw_image_to_svg(
                    buf.getvalue(),
                    img_format="png",
                    colormode="color",
                    hierarchical="stacked",
                    mode="spline",
                    filter_speckle=4,
                    color_precision=6,
                    layer_difference=16,
                    corner_threshold=60,
                    length_threshold=4.0,
                    splice_threshold=45,
                )
                self.root.after(0, show)
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Vectorization failed", str(e)))

        def show():
            bar.stop()
            bar.destroy()
            status_var.set("Vectorization complete.")
            preview = _svg_preview(self.svg_string) or self.clamped_image
            ph = self._photo(preview, max_size=440)
            ttk.Label(frame, image=ph).pack(pady=6)
            self._nav_row(frame, back_cmd=self.step7_clamp_colors,
                          fwd_text="Check & Save →", fwd_cmd=self.step9_thin_check)

        threading.Thread(target=run, daemon=True).start()

    # ── Step 9: Thin check + save ──────────────────────────────────────────────

    def step9_thin_check(self):
        frame = self._new_frame()
        min_mm = self.settings["min_thickness_mm"]
        self._header(frame, "Step 8 of 8 — Thin Check & Save",
                     f"Removing features thinner than {min_mm} mm, then save color layers.")

        bar = ttk.Progressbar(frame, mode="indeterminate", length=380)
        bar.pack(pady=10)
        bar.start(12)
        status_var = tk.StringVar(value="Checking thin features…")
        ttk.Label(frame, textvariable=status_var).pack()

        def run():
            import tempfile
            from thinchecker import process_svg as thin_process

            checked_svg = self.svg_string
            try:
                with tempfile.NamedTemporaryFile(
                    suffix=".svg", delete=False, mode="w", encoding="utf-8"
                ) as f_in:
                    f_in.write(self.svg_string)
                    in_path = f_in.name
                out_path = in_path.replace(".svg", "_checked.svg")
                thin_process(in_path, out_path, min_mm, simplify_tol_mm=0.1)
                with open(out_path, "r", encoding="utf-8") as f:
                    checked_svg = f.read()
                os.unlink(in_path)
                os.unlink(out_path)
                self.root.after(0, lambda: status_var.set(
                    f"Thin regions removed. Ready to save."))
            except Exception as e:
                self.root.after(0, lambda: status_var.set(f"Thin check skipped: {e}"))

            self.root.after(0, lambda svg=checked_svg: show(svg))

        def show(final_svg: str):
            bar.stop()
            bar.destroy()
            preview = _svg_preview(final_svg) or self.clamped_image
            ph = self._photo(preview, max_size=440)
            ttk.Label(frame, image=ph).pack(pady=6)

            def save():
                path = filedialog.asksaveasfilename(
                    title="Save SVG",
                    defaultextension=".svg",
                    filetypes=[("SVG files", "*.svg"), ("All files", "*.*")],
                )
                if path:
                    _save_svg_layers(
                        final_svg, path,
                        self.settings["width_mm"],
                        self.settings["height_mm"],
                    )
                    self._reset_session()

            self._nav_row(frame, back_cmd=self.step8_vectorize,
                          fwd_text="Save SVG…", fwd_cmd=save)

        threading.Thread(target=run, daemon=True).start()


# ── Module-level helpers ───────────────────────────────────────────────────────

def _svg_preview(svg_string: str) -> Image.Image | None:
    """Rasterize SVG to a PIL Image for in-app preview. Returns None if unavailable."""
    try:
        import cairosvg
        png_bytes = cairosvg.svg2png(bytestring=svg_string.encode(), output_width=512)
        img = Image.open(io.BytesIO(png_bytes))
        img.load()
        return img
    except Exception:
        return None


def _save_svg_layers(svg_string: str, base_path: str,
                     width_mm: float, height_mm: float):
    """Save the SVG with physical mm dimensions stamped onto it."""
    from lxml import etree

    base = Path(base_path)
    try:
        root = etree.fromstring(svg_string.encode())
        vb = root.get("viewBox") or (
            f"0 0 {root.get('width', '100')} {root.get('height', '100')}"
        )
        root.set("width",   f"{width_mm}mm")
        root.set("height",  f"{height_mm}mm")
        root.set("viewBox", vb)
        base.write_bytes(etree.tostring(root, pretty_print=True,
                                        xml_declaration=True, encoding="UTF-8"))
    except Exception:
        base.write_text(svg_string, encoding="utf-8")

    messagebox.showinfo("Saved", f"Saved:\n{base_path}")


def main():
    app = App()
    app.root.mainloop()


if __name__ == "__main__":
    main()
