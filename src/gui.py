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


PREVIEW_MAX = 1024

_RESOURCES = Path(__file__).parent.parent / "resources"

_PRESET_COLORS = ["white", "grey", "black",
                  "cyan", "magenta", "yellow",
                  "red", "orange", "green", "blue", "purple"]

_CONFIG_DEFAULTS: dict = {
    "max_dimension":      512,
    "width_mm":           250,
    "height_mm":          200,
    "min_thickness_mm":   1.5,
    "attempts":           4,
    "colors":             ["black", "white", "red"],
    "background_color":   "white",
    "special_requests":   "",
    "vectorizer":         "potrace",
    "api_base":           "http://localhost:5001",
    "prompt_template": (
        "Convert the reference image into a bold high contrast graphic design "
        "using only {colors} suitable for vinyl cutting. "
        "Flat solid shapes, no gradients, no fine detail, clean sharp edges, "
        "white background, style of a screen print or sticker design."
    ),
    "generation_params":   {"cfg_scale": 1, "steps": 6, "sampler_name": "Euler", "seed": -1},
    "potrace_params":      {"turdsize": 2, "alphamax": 1.0, "opticurve": True, "opttolerance": 0.2},
    "opencv_params":       {"approx_epsilon": 1.5},
    "thinchecker_params":  {"simplify_tol_mm": 0.1},
}

def _load_config() -> dict:
    import json
    cfg = dict(_CONFIG_DEFAULTS)
    path = _RESOURCES / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for key, default in _CONFIG_DEFAULTS.items():
            if key not in data:
                continue
            if isinstance(default, dict):
                # Merge so extra keys in config flow through as kwargs
                merged = dict(default)
                merged.update(data[key])
                cfg[key] = merged
            else:
                cfg[key] = data[key]
    except Exception as e:
        print(f"Warning: could not load config.json: {e}", file=sys.stderr)
    return cfg

CONFIG: dict = _load_config()


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("TransferMaker")
        self.root.minsize(920, 680)

        # State shared across steps
        self.source_image_path: str | None = None
        self.processed_b64: str | None = None
        self.processed_pil: Image.Image | None = None
        self.settings: dict = {}
        self.generated_images: list[Image.Image] = []
        self.selected_image: Image.Image | None = None
        self.upscaled_image: Image.Image | None = None
        self.color_layers: list[tuple[str, tuple, Image.Image]] = []  # (name, rgb, mask)
        self.clamped_image: Image.Image | None = None
        self.svg_string: str | None = None

        self._photo_refs: list = []
        self._step_frame: ttk.Frame | None = None
        self.task: str | None = None
        self._clamp_phase: str = "normal"

        self.container = ttk.Frame(self.root, padding=16)
        self.container.pack(fill=tk.BOTH, expand=True)

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
        """Clear all session state and return to task selection."""
        self.source_image_path = None
        self.processed_b64 = None
        self.processed_pil = None
        self.settings = {}
        self.generated_images = []
        self.selected_image = None
        self.upscaled_image = None
        self.color_layers = []
        self.clamped_image = None
        self.svg_string = None
        self.task = None
        self._clamp_phase = "normal"
        self.step_task_select()

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

    def _build_prompt(self, extra: str = "") -> str:
        colors = self.settings["colors"]
        color_list = (", ".join(colors[:-1]) + f" and {colors[-1]}"
                      if len(colors) > 1 else colors[0])
        prompt = CONFIG["prompt_template"].format(
            colors=color_list,
            background_color=self.settings["background_color"],
        )
        if extra:
            prompt += f" {extra}"
        return prompt

    def _call_img2img(self, prompt: str, width: int, height: int, b64: str) -> Image.Image:
        payload = {
            **CONFIG["generation_params"],
            "prompt": prompt,
            "width":  width,
            "height": height,
            "extra_images": [b64],
        }
        r = requests.post(f"{CONFIG['api_base']}/sdapi/v1/img2img", json=payload, timeout=600)
        r.raise_for_status()
        img = Image.open(io.BytesIO(base64.b64decode(r.json()["images"][0])))
        img.load()
        return img

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
                    r = requests.get(f"{CONFIG["api_base"]}/sdapi/v1/options", timeout=3)
                    if r.status_code == 200:
                        self.root.after(0, self.step_task_select)
                        return
                except Exception:
                    pass
                self.root.after(0, lambda: status_var.set("Waiting for model to load…"))
                time.sleep(2)

        threading.Thread(target=poll, daemon=True).start()

    # ── Task selection ─────────────────────────────────────────────────────────

    def step_task_select(self):
        frame = self._new_frame()
        self._header(frame, "TransferMaker — Select Task",
                     "Choose what you would like to do.")

        tasks = [
            ("Turn a Photo into a Transfer",
             "photo_to_vectors",
             "Transform a reference photo and a set of colors into vector objects ready for transfer."),
            ("Duplicate an Existing Design",
             "duplicate",
             "Generate an SVG from a picture of an existing design\n"
             "Pick colors from the image and attempt a quick clone, with the option to use an AI after viewing the result."),
            ("Clamp Colors in an Image",
             "clamp_only",
             "Provide an image and specify a set of colors you will get a vector image using only those colors suitable for transfer."),
        ]

        task_var = tk.StringVar(value="")

        for label, value, desc in tasks:
            row = ttk.Frame(frame)
            row.pack(anchor="w", pady=(8, 0), padx=20)
            ttk.Radiobutton(row, text=label, variable=task_var,
                            value=value, width=28).pack(side=tk.LEFT)
            ttk.Label(row, text=desc, foreground="gray",
                      wraplength=460, justify="left").pack(side=tk.LEFT, padx=(8, 0))

        def go():
            t = task_var.get()
            if not t:
                messagebox.showinfo("No selection", "Please select a task first.")
                return
            self.task = t
            self.step2_upload_image()

        self._nav_row(frame, fwd_text="Next →", fwd_cmd=go)

    # ── Duplicate / Fix — check suitability ────────────────────────────────────

    def step_dup_check_suitability(self):
        """Review image info and confirm suitability (duplicate / fix_modify flows)."""
        frame = self._new_frame()
        task_label = ("Duplicate Design" if self.task == "duplicate"
                      else "Fix or Modify Design")
        self._header(frame, f"{task_label} — Check Image",
                     "Review image details and confirm it is suitable to proceed.")

        ph = self._photo(self.processed_pil, max_size=320)
        ttk.Label(frame, image=ph).pack(pady=6)

        img = self.processed_pil
        mode_desc = {"RGB": "Full colour", "RGBA": "Full colour + transparency",
                     "L": "Greyscale", "P": "Palette"}.get(img.mode, img.mode)
        info = (f"Size: {img.width} × {img.height} px\n"
                f"Mode: {mode_desc}\n"
                f"File: {Path(self.source_image_path).name}")
        ttk.Label(frame, text=info, justify="left", foreground="gray").pack(pady=4)

        self._nav_row(frame,
                      back_cmd=self.step3_confirm_image,
                      fwd_text="Looks good →",
                      fwd_cmd=self.step_color_picker)

    # ── Unified colour picker (all tasks) ─────────────────────────────────────

    def step_color_picker(self):
        """Image-based colour picker used by every task flow."""
        frame = self._new_frame()

        if self.task == "duplicate":
            title, desc = "Duplicate Design — Pick Colours", \
                          "Click the image to sample a colour into the active slot. Scroll to zoom."
        elif self.task == "fix_modify":
            title, desc = "Fix or Modify Design — Pick Colours", \
                          "Click the image to sample a colour into the active slot. Scroll to zoom."
        elif self.task == "clamp_only":
            title, desc = "Clamp Colors in Design — Pick Colours", \
                          "Click the image to sample a colour into the active slot. Scroll to zoom."
        else:  # photo_to_vectors
            title, desc = "Step 3 of 8 — Colours & Settings", \
                          "Click the image to sample a colour. Set dimensions and generation options."
        self._header(frame, title, desc)

        orig_img = Image.open(self.source_image_path).convert("RGB")

        # ── Layout: image canvas left, controls right ──────────────────────────
        panes = ttk.Frame(frame)
        panes.pack(fill=tk.BOTH, expand=True)

        img_outer = ttk.Frame(panes)
        img_outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        hbar = ttk.Scrollbar(img_outer, orient="horizontal")
        vbar = ttk.Scrollbar(img_outer, orient="vertical")
        img_canvas = tk.Canvas(img_outer, bg="#1a1a1a", cursor="crosshair",
                               xscrollcommand=hbar.set, yscrollcommand=vbar.set)
        hbar.config(command=img_canvas.xview)
        vbar.config(command=img_canvas.yview)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        hbar.pack(side=tk.BOTTOM, fill=tk.X)
        img_canvas.pack(fill=tk.BOTH, expand=True)

        zoom_level = [1.0]
        _ph_ref = [None]

        def _render():
            nw = max(1, int(orig_img.width  * zoom_level[0]))
            nh = max(1, int(orig_img.height * zoom_level[0]))
            resample = (Image.Resampling.NEAREST if zoom_level[0] > 1.5
                        else Image.Resampling.LANCZOS)
            scaled = orig_img.resize((nw, nh), resample)
            ph = ImageTk.PhotoImage(scaled)
            _ph_ref[0] = ph
            img_canvas.delete("all")
            img_canvas.create_image(0, 0, anchor="nw", image=ph)
            img_canvas.configure(scrollregion=(0, 0, nw, nh))

        def _zoom(event):
            factor = 1.15 if event.delta > 0 else (1 / 1.15)
            zoom_level[0] = max(0.1, min(zoom_level[0] * factor, 16.0))
            _render()

        img_canvas.bind("<Configure>", lambda e: _render())
        img_canvas.bind("<MouseWheel>", _zoom)
        img_canvas.bind("<Button-4>", lambda e: _zoom(type("_", (), {"delta":  1})()))
        img_canvas.bind("<Button-5>", lambda e: _zoom(type("_", (), {"delta": -1})()))

        # ── Controls panel ─────────────────────────────────────────────────────
        ctrl = ttk.Frame(panes, padding=(12, 0, 0, 0), width=240)
        ctrl.pack(side=tk.RIGHT, fill=tk.Y)
        ctrl.pack_propagate(False)

        # ── Slot system ────────────────────────────────────────────────────────
        n_row = ttk.Frame(ctrl)
        n_row.pack(anchor="w", pady=(0, 4))
        ttk.Label(n_row, text="Design colors:").pack(side=tk.LEFT, padx=(0, 6))
        n_var = tk.IntVar(value=4)

        slots_frame = ttk.LabelFrame(ctrl, text="Slots  (click slot → click image)")
        slots_frame.pack(fill=tk.X)

        color_slots: list[tuple[tk.StringVar, tk.Label, ttk.Frame]] = []
        active_slot = [1]

        def _highlight_slots():
            for j, (_, _, row) in enumerate(color_slots):
                row.configure(relief="solid" if j == active_slot[0] else "flat",
                              borderwidth=2 if j == active_slot[0] else 1)

        def _set_active(i: int):
            active_slot[0] = i
            _highlight_slots()

        def _refresh_swatch(var: tk.StringVar, swatch: tk.Label):
            from PIL import ImageColor
            try:
                r, g, b = ImageColor.getrgb(var.get().strip())[:3]
                swatch.configure(bg=f"#{r:02x}{g:02x}{b:02x}")
            except Exception:
                swatch.configure(bg="#888888")

        def _rgb_dist(a, b):
            return ((a[0]-b[0])**2 + (a[1]-b[1])**2 + (a[2]-b[2])**2) ** 0.5

        def _pick_best_bg():
            """Set BG slot to the preset color most distant from all design colors."""
            from PIL import ImageColor
            design_rgbs = []
            for v, _, _ in color_slots[1:]:
                try:
                    design_rgbs.append(ImageColor.getrgb(v.get().strip())[:3])
                except Exception:
                    pass
            if not design_rgbs:
                return
            best_name, best_dist = _PRESET_COLORS[0], -1.0
            for name in _PRESET_COLORS:
                try:
                    rgb = ImageColor.getrgb(name)[:3]
                    min_d = min(_rgb_dist(rgb, d) for d in design_rgbs)
                    if min_d > best_dist:
                        best_dist, best_name = min_d, name
                except Exception:
                    pass
            color_slots[0][0].set(best_name)

        _BG_VALUES = ["Automatic"] + _PRESET_COLORS

        def _add_slot(i: int, default: str = ""):
            is_bg = (i == 0)
            row = ttk.Frame(slots_frame, relief="flat", borderwidth=1)
            row.pack(fill=tk.X, pady=1, padx=2)

            lbl = ttk.Label(row, text="BG" if is_bg else f"{i}.", width=3)
            if is_bg:
                lbl.configure(foreground="#0066cc")
            lbl.pack(side=tk.LEFT, padx=(4, 2))

            swatch = tk.Label(row, width=3, bg="#888888", relief="sunken")
            swatch.pack(side=tk.LEFT, padx=(0, 4), pady=3)

            var = tk.StringVar(value=default)
            if is_bg:
                combo = ttk.Combobox(row, textvariable=var, values=_BG_VALUES,
                                     width=10, state="readonly")
            else:
                combo = ttk.Combobox(row, textvariable=var, values=_PRESET_COLORS, width=10)
            combo.pack(side=tk.LEFT, padx=(0, 4))

            if is_bg:
                ttk.Button(row, text="Auto", width=4,
                           command=_pick_best_bg).pack(side=tk.LEFT, padx=(0, 2))

            var.trace_add("write", lambda *_, v=var, s=swatch: _refresh_swatch(v, s))
            _refresh_swatch(var, swatch)

            for w in (row, lbl, swatch):
                w.bind("<Button-1>", lambda e, idx=i: _set_active(idx))
            combo.bind("<FocusIn>", lambda e, idx=i: _set_active(idx))
            combo.bind("<Button-1>", lambda e, idx=i: _set_active(idx))
            color_slots.append((var, swatch, row))

        def _build_slots(n: int):
            total = 1 + n
            current = len(color_slots)
            if total < current:
                for i in range(current - 1, total - 1, -1):
                    color_slots[i][2].destroy()
                    color_slots.pop(i)
            else:
                for i in range(current, total):
                    _add_slot(i, "Automatic" if i == 0 else "")
            active_slot[0] = max(1, min(active_slot[0], total - 1))
            _highlight_slots()

        def _on_n_change(*_):
            try:
                n = int(n_var.get())
                if 1 <= n <= 8:
                    _build_slots(n)
            except (ValueError, tk.TclError):
                pass

        ttk.Spinbox(n_row, textvariable=n_var, from_=1, to=8, width=4,
                    command=_on_n_change).pack(side=tk.LEFT)
        n_var.trace_add("write", _on_n_change)
        _build_slots(4)

        # ── Click canvas → sample pixel ────────────────────────────────────────
        def _pick(event):
            cx = img_canvas.canvasx(event.x)
            cy = img_canvas.canvasy(event.y)
            ox = max(0, min(int(cx / zoom_level[0]), orig_img.width  - 1))
            oy = max(0, min(int(cy / zoom_level[0]), orig_img.height - 1))
            r, g, b = orig_img.getpixel((ox, oy))
            hex_col = f"#{r:02x}{g:02x}{b:02x}"
            i = active_slot[0]
            if 0 <= i < len(color_slots):
                color_slots[i][0].set(hex_col)
                for j in range(i + 1, len(color_slots)):
                    if not color_slots[j][0].get():
                        _set_active(j)
                        return
                _set_active(len(color_slots) - 1)

        img_canvas.bind("<Button-1>", _pick)

        # ── Dimension fields ───────────────────────────────────────────────────
        ttk.Separator(ctrl, orient="horizontal").pack(fill=tk.X, pady=(10, 6))

        def dim_field(label, default):
            ttk.Label(ctrl, text=label).pack(anchor="w")
            var = tk.StringVar(value=str(default))
            ttk.Entry(ctrl, textvariable=var).pack(fill=tk.X, pady=(0, 5))
            return var

        width_var  = dim_field("Output width (mm):",   CONFIG["width_mm"])
        height_var = dim_field("Output height (mm):",  CONFIG["height_mm"])
        thick_var  = dim_field("Min thickness (mm):",  CONFIG["min_thickness_mm"])

        # ── photo_to_vectors extras ────────────────────────────────────────────
        attempts_var  = None
        requests_box  = None
        if self.task == "photo_to_vectors":
            ttk.Separator(ctrl, orient="horizontal").pack(fill=tk.X, pady=(6, 6))
            att_row = ttk.Frame(ctrl)
            att_row.pack(anchor="w", pady=(0, 5))
            ttk.Label(att_row, text="Attempts:").pack(side=tk.LEFT, padx=(0, 6))
            attempts_var = tk.StringVar(value=str(CONFIG["attempts"]))
            ttk.Spinbox(att_row, textvariable=attempts_var,
                        from_=1, to=8, width=4).pack(side=tk.LEFT)
            ttk.Label(ctrl, text="Special requests:").pack(anchor="w")
            requests_box = tk.Text(ctrl, height=3, wrap=tk.WORD)
            requests_box.pack(fill=tk.X, pady=(0, 5))
            if CONFIG["special_requests"]:
                requests_box.insert("1.0", CONFIG["special_requests"])

        # ── Confirm ────────────────────────────────────────────────────────────
        def go():
            try:
                w = float(width_var.get())
                h = float(height_var.get())
                t = float(thick_var.get())
                assert w > 0 and h > 0 and t > 0
            except Exception:
                messagebox.showerror("Invalid input",
                                     "Please check width, height and thickness.")
                return
            from PIL import ImageColor
            colors = [v.get().strip() for v, _, _ in color_slots[1:] if v.get().strip()]
            if not colors:
                messagebox.showerror("No design colours",
                                     "Pick at least one design colour.")
                return
            if color_slots[0][0].get().strip().lower() == "automatic":
                _pick_best_bg()
            bg_color = color_slots[0][0].get().strip() or "white"
            try:
                bg_rgb = ImageColor.getrgb(bg_color)[:3]
                close = [c for c in colors if _rgb_dist(bg_rgb, ImageColor.getrgb(c)[:3]) < 80]
                if close:
                    if not messagebox.askyesno(
                            "Background too similar",
                            f"Background '{bg_color}' is very close to: {', '.join(close)}.\n"
                            "This will cause those colors to clamp together.\n\n"
                            "Continue anyway?"):
                        return
            except Exception:
                pass
            self.settings = {
                "width_mm": w, "height_mm": h, "min_thickness_mm": t,
                "colors": colors, "background_color": bg_color,
                "attempts": CONFIG["attempts"], "special_requests": "",
            }
            if self.task == "photo_to_vectors":
                try:
                    a = int(attempts_var.get())
                    assert 1 <= a <= 8
                except Exception:
                    messagebox.showerror("Invalid input", "Attempts must be 1–8.")
                    return
                self.settings["attempts"] = a
                self.settings["special_requests"] = requests_box.get("1.0", tk.END).strip()
                self.step5_generate()
            elif self.task == "clamp_only":
                self.selected_image = self.processed_pil
                self._clamp_phase = "clamp_only"
                self.step7_clamp_colors()
            else:  # duplicate / fix_modify
                self.selected_image = self.processed_pil
                self._clamp_phase = ("first_dup" if self.task == "duplicate"
                                     else "first_fix")
                self.step7_clamp_colors()

        def _back():
            if self.task in ("duplicate", "fix_modify"):
                self.step_dup_check_suitability()
            else:
                self.step3_confirm_image()

        fwd_text = "Generate →" if self.task == "photo_to_vectors" else "Clamp →"
        self._nav_row(frame, back_cmd=_back, fwd_text=fwd_text, fwd_cmd=go)

    # ── Duplicate — post-clamp review ─────────────────────────────────────────

    def step_dup_post_clamp(self):
        """Ask whether the clamped result is good enough to vectorize (duplicate)."""
        frame = self._new_frame()
        self._header(frame, "Duplicate Design — Review Clamped Result",
                     "Is the colour-clamped design ready for vinyl cutting?")

        ph = self._photo(self.clamped_image, max_size=500)
        ttk.Label(frame, image=ph).pack(pady=6)

        row = ttk.Frame(frame)
        row.pack(side=tk.BOTTOM, anchor="w", pady=12)
        ttk.Button(row, text="← Back",
                   command=self.step_color_picker).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(row, text="Yes — Vectorize →",
                   command=self.step8_vectorize).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(row, text="No — Improve with Diffusion →",
                   command=self.step_dup_generate_settings).pack(side=tk.LEFT)

    # ── Duplicate / Fix — generation settings ─────────────────────────────────

    def step_dup_generate_settings(self):
        """Collect prompt and attempts before running diffusion (duplicate / fix_modify)."""
        frame = self._new_frame()
        task_label = ("Duplicate Design" if self.task == "duplicate"
                      else "Fix or Modify Design")
        default_instructions = (
            "Reproduce this design faithfully for vinyl cutting. "
            "Bold high contrast, flat solid shapes, no gradients, clean sharp edges."
            if self.task == "duplicate" else ""
        )
        self._header(frame, f"{task_label} — Generation Instructions",
                     "Describe how to transform the image, then generate.")

        form = ttk.Frame(frame, padding=(40, 0))
        form.pack(fill=tk.X)
        ttk.Label(form, text="Instructions / custom prompt:").pack(anchor="w")
        instructions_box = tk.Text(form, height=4, wrap=tk.WORD, width=60)
        instructions_box.pack(fill=tk.X, pady=(2, 8))
        if default_instructions:
            instructions_box.insert("1.0", default_instructions)

        att_row = ttk.Frame(form)
        att_row.pack(anchor="w")
        ttk.Label(att_row, text="Variations to generate:").pack(side=tk.LEFT, padx=(0, 8))
        attempts_var = tk.StringVar(value=str(CONFIG["attempts"]))
        ttk.Spinbox(att_row, textvariable=attempts_var, from_=1, to=8,
                    width=5).pack(side=tk.LEFT)

        back_dest = (self.step_dup_post_clamp if self.task == "duplicate"
                     else self.step_color_picker)

        def go():
            instructions = instructions_box.get("1.0", tk.END).strip()
            try:
                attempts = int(attempts_var.get())
                assert 1 <= attempts <= 8
            except Exception:
                messagebox.showerror("Invalid input", "Attempts must be 1–8.")
                return
            self.settings["special_requests"] = instructions
            self.settings["attempts"] = attempts
            # After generation + pick, the subsequent clamp routes normally
            self._clamp_phase = "normal"
            self.step5_generate()

        self._nav_row(frame, back_cmd=back_dest,
                      fwd_text="Generate →", fwd_cmd=go)

    # ── Step 2: Upload image ───────────────────────────────────────────────────

    def step2_upload_image(self):
        frame = self._new_frame()
        self._header(frame, "Step 1 of 8 — Upload Image",
                     "Choose a reference photo or artwork to convert into a vinyl transfer design.")

        preview_lbl = ttk.Label(frame)
        preview_lbl.pack(pady=6)
        info_var = tk.StringVar(value="No image selected.")
        ttk.Label(frame, textvariable=info_var, foreground="gray").pack()

        next_btn = self._nav_row(frame, back_cmd=self.step_task_select,
                                 fwd_text="Next →", fwd_state="disabled")

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
                processor = ImageProcessor(max_dimension=CONFIG["max_dimension"])
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

        def _fwd():
            if self.task == "duplicate" or self.task == "fix_modify":
                self.step_dup_check_suitability()
            else:
                self.step_color_picker()

        self._nav_row(frame, back_cmd=self.step2_upload_image,
                      fwd_text="Confirm →", fwd_cmd=_fwd)

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
        canvas = tk.Canvas(frame, height=280, highlightthickness=0)
        hscroll = ttk.Scrollbar(frame, orient="horizontal", command=canvas.xview)
        canvas.configure(xscrollcommand=hscroll.set)
        canvas.pack(fill=tk.X, pady=6)
        hscroll.pack(fill=tk.X)
        thumb_row = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=thumb_row, anchor="nw")
        thumb_row.bind("<Configure>",
                       lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        self.generated_images = []
        prompt = self._build_prompt(self.settings["special_requests"])
        src_b64 = self.processed_b64
        src_w, src_h = self.processed_pil.width, self.processed_pil.height

        def run():
            for i in range(attempts):
                self.root.after(0, lambda i=i: status_var.set(
                    f"Generating image {i + 1} of {attempts}…"))
                try:
                    img = self._call_img2img(prompt, src_w, src_h, src_b64)
                    self.generated_images.append(img)
                    idx = len(self.generated_images) - 1

                    def add_thumb(img=img, idx=idx):
                        ph = self._photo(img, max_size=240)
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
            settings_back = (self.step_dup_generate_settings
                             if self.task in ("duplicate", "fix_modify")
                             else self.step_color_picker)
            ttk.Button(frame, text="← Change settings",
                       command=settings_back).pack()

        threading.Thread(target=run, daemon=True).start()

    # ── Step 6: Pick image ─────────────────────────────────────────────────────

    def step6_pick_image(self):
        frame = self._new_frame()
        self._header(frame, "Step 5 of 8 — Pick a Design",
                     "Click a design to select it, then click Continue.")

        selected_idx = tk.IntVar(value=-1)
        cells: list[ttk.Frame] = []

        cols = min(2, len(self.generated_images))

        # Vertically scrollable canvas for the image grid
        outer = ttk.Frame(frame)
        outer.pack(fill=tk.BOTH, expand=True, pady=6)
        grid_canvas = tk.Canvas(outer, highlightthickness=0)
        vscroll = ttk.Scrollbar(outer, orient="vertical", command=grid_canvas.yview)
        grid_canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        grid_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        grid_frame = ttk.Frame(grid_canvas)
        grid_canvas.create_window((0, 0), window=grid_frame, anchor="nw")
        grid_frame.bind("<Configure>",
                        lambda e: grid_canvas.configure(scrollregion=grid_canvas.bbox("all")))

        for idx, img in enumerate(self.generated_images):
            ph = self._photo(img, max_size=300)
            cell = ttk.Frame(grid_frame, relief="flat", borderwidth=2)
            cell.grid(row=idx // cols, column=idx % cols, padx=8, pady=8)
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

        def _require_selection() -> int:
            i = selected_idx.get()
            if i < 0:
                messagebox.showinfo("No selection", "Click an image to select it first.")
            return i

        def clamp():
            i = _require_selection()
            if i < 0:
                return
            self.selected_image = self.generated_images[i]
            self.step7_clamp_colors()

        def edit():
            i = _require_selection()
            if i < 0:
                return
            self.step6_edit_image(self.generated_images[i])

        row = ttk.Frame(frame)
        row.pack(side=tk.BOTTOM, anchor="w", pady=12)
        ttk.Button(row, text="← Back", command=self.step5_generate).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(row, text="Edit…",   command=edit).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(row, text="Clamp →", command=clamp).pack(side=tk.LEFT)

    # ── Step 6b: Edit selected image ───────────────────────────────────────────

    def step6_edit_image(self, source_image: "Image.Image"):
        frame = self._new_frame()
        self._header(frame, "Step 5 of 8 — Edit Design",
                     "Describe changes to apply to the selected image, then generate new options.")

        # Preview of the image being edited
        ph = self._photo(source_image, max_size=300)
        ttk.Label(frame, image=ph).pack(pady=(0, 8))

        form = ttk.Frame(frame)
        form.pack(fill=tk.X, padx=40)

        ttk.Label(form, text="Edit instructions:").pack(anchor="w")
        instructions_box = tk.Text(form, height=3, wrap=tk.WORD, width=60)
        instructions_box.pack(fill=tk.X, pady=(2, 8))

        att_row = ttk.Frame(form)
        att_row.pack(anchor="w")
        ttk.Label(att_row, text="Variations to generate:").pack(side=tk.LEFT, padx=(0, 8))
        attempts_var = tk.StringVar(value=str(self.settings.get("attempts", CONFIG["attempts"])))
        ttk.Spinbox(att_row, textvariable=attempts_var, from_=1, to=8, width=5).pack(side=tk.LEFT)

        status_var = tk.StringVar(value="")
        status_lbl = ttk.Label(frame, textvariable=status_var)
        status_lbl.pack(pady=(8, 0))
        bar = ttk.Progressbar(frame, length=440)
        bar.pack(pady=4)

        nav_row = ttk.Frame(frame)
        nav_row.pack(side=tk.BOTTOM, anchor="w", pady=12)
        back_btn = ttk.Button(nav_row, text="← Back", command=self.step6_pick_image)
        back_btn.pack(side=tk.LEFT, padx=(0, 6))

        def start():
            instructions = instructions_box.get("1.0", tk.END).strip()
            if not instructions:
                messagebox.showinfo("No instructions", "Describe what to change before generating.")
                return
            try:
                attempts = int(attempts_var.get())
                assert 1 <= attempts <= 8
            except Exception:
                messagebox.showerror("Invalid input", "Attempts must be 1–8.")
                return

            generate_btn.configure(state="disabled")
            back_btn.configure(state="disabled")
            bar.configure(maximum=attempts)
            bar["value"] = 0

            prompt = self._build_prompt(instructions)
            buf = io.BytesIO()
            source_image.save(buf, format="PNG")
            src_b64 = base64.b64encode(buf.getvalue()).decode()

            new_images: list = []

            def run():
                for i in range(attempts):
                    self.root.after(0, lambda i=i: status_var.set(
                        f"Generating variation {i + 1} of {attempts}…"))
                    try:
                        img = self._call_img2img(
                            prompt, source_image.width, source_image.height, src_b64)
                        new_images.append(img)
                        self.root.after(0, lambda: bar.step(1))
                    except Exception as e:
                        err = str(e)
                        self.root.after(0, lambda err=err: status_var.set(f"Error: {err}"))
                self.root.after(0, lambda: done(new_images))

            def done(imgs):
                if not imgs:
                    status_var.set("No images were generated.")
                    generate_btn.configure(state="normal")
                    back_btn.configure(state="normal")
                    return
                self.generated_images = imgs
                self.step6_pick_image()

            threading.Thread(target=run, daemon=True).start()

        generate_btn = ttk.Button(nav_row, text="Generate →", command=start)
        generate_btn.pack(side=tk.LEFT)

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
            import numpy as np
            from PIL import ImageColor

            # ── Upscale selected image ─────────────────────────────────────────
            upscaled = self.selected_image
            try:
                buf = io.BytesIO()
                self.selected_image.save(buf, format="PNG")
                up = requests.post(
                    f"{CONFIG["api_base"]}/sdapi/v1/upscale",
                    json={"image": base64.b64encode(buf.getvalue()).decode(),
                          "upscaling_resize": 4},
                    timeout=300,
                )
                if up.status_code == 200:
                    upscaled = Image.open(io.BytesIO(base64.b64decode(up.json()["image"])))
                    upscaled.load()
            except Exception as e:
                self.root.after(0, lambda: status_var.set(f"Upscale failed ({e}), using original."))

            self.upscaled_image = upscaled
            self.root.after(0, lambda: status_var.set("Clamping colors…"))

            # ── Build ordered (name, rgb) pairs — background color always first ───────────
            color_specs: list[tuple[str, tuple]] = []
            seen: set = set()

            def try_add(name, rgb):
                if rgb not in seen:
                    seen.add(rgb)
                    color_specs.append((name, rgb))

            bg_name = self.settings["background_color"]
            try:
                bg_rgb = ImageColor.getrgb(bg_name.strip())[:3]
            except Exception:
                bg_rgb = (255, 255, 255)
            try_add(bg_name, bg_rgb)
            for name in colors:
                try:
                    try_add(name, ImageColor.getrgb(name.strip())[:3])
                except Exception:
                    pass

            # ── Quantize to the exact palette ──────────────────────────────────
            from mrf_quantize import mrf_quantize

            _, self.clamped_image, self.color_layers = mrf_quantize(
                upscaled,
                color_specs,
                alpha=8.0,      # ↑ simpler cuts, fewer fragments to weed
                beta=40.0,       # ↑ boundaries snap harder to drawn edges
                sigma_color=50.0,
                sigma_pair=30.0,
            )
            self.root.after(0, show)

        def show():
            bar.stop()
            bar.destroy()
            pair = ttk.Frame(frame)
            pair.pack(pady=6)
            upscaled_src = self.upscaled_image or self.selected_image
            for label, src in [("Upscaled", upscaled_src),
                                ("Color Clamped", self.clamped_image)]:
                cell = ttk.Frame(pair)
                cell.pack(side=tk.LEFT, padx=16)
                ph = self._photo(src, max_size=360)
                ttk.Label(cell, image=ph).pack()
                ttk.Label(cell, text=label).pack()
            phase = self._clamp_phase
            if phase == "first_dup":
                self._nav_row(frame, back_cmd=self.step_color_picker,
                              fwd_text="Review →", fwd_cmd=self.step_dup_post_clamp)
            elif phase == "first_fix":
                self._nav_row(frame, back_cmd=self.step_color_picker,
                              fwd_text="Generate →", fwd_cmd=self.step_dup_generate_settings)
            elif phase == "clamp_only":
                self._nav_row(frame, back_cmd=self.step_color_picker,
                              fwd_text="Vectorize →", fwd_cmd=self.step8_vectorize)
            else:
                self._nav_row(frame, back_cmd=self.step6_pick_image,
                              fwd_text="Vectorize →", fwd_cmd=self.step8_vectorize)

        threading.Thread(target=run, daemon=True).start()

    # ── Step 8: Vectorize ──────────────────────────────────────────────────────

    def step8_vectorize(self):
        frame = self._new_frame()
        self._header(frame, "Step 7 of 8 — Vectorization",
                     "Tracing image into SVG paths for vinyl cutting.")

        # Progress widgets (hidden until tracing starts)
        progress_frame = ttk.Frame(frame)
        bar = ttk.Progressbar(progress_frame, mode="indeterminate", length=380)
        bar.pack()
        status_var = tk.StringVar(value="")
        ttk.Label(progress_frame, textvariable=status_var).pack(pady=4)

        nav = self._nav_row(frame, back_cmd=self.step7_clamp_colors,
                            fwd_text="Vectorize →",
                            fwd_cmd=lambda: _start())

        def _start():
            nav.configure(state="disabled")
            progress_frame.pack(pady=10)
            bar.start(12)
            status_var.set("Starting…")
            threading.Thread(target=run, daemon=True).start()

        def run():
            try:
                import numpy as np
                from lxml import etree

                SVGNS = "http://www.w3.org/2000/svg"
                INK   = "http://www.inkscape.org/namespaces/inkscape"

                W, H = self.clamped_image.size
                total = len(self.color_layers)

                root_svg = etree.Element(
                    f"{{{SVGNS}}}svg",
                    nsmap={None: SVGNS, "inkscape": INK},
                )
                root_svg.set("width",   str(W))
                root_svg.set("height",  str(H))
                root_svg.set("viewBox", f"0 0 {W} {H}")

                for i, (name, rgb, mask_img) in enumerate(self.color_layers):
                    self.root.after(0, lambda i=i, n=name: status_var.set(
                        f"Tracing layer {i + 1}/{total}: {n}…"))

                    arr       = np.array(mask_img)
                    # 255 = foreground (this colour's pixels) for both engines
                    binary    = np.where(arr == 0, 255, 0).astype(np.uint8)
                    hex_color = f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"

                    group = etree.SubElement(root_svg, f"{{{SVGNS}}}g")
                    group.set("id",                  f"layer_{name.replace(' ', '_')}")
                    group.set(f"{{{INK}}}label",     name)
                    group.set(f"{{{INK}}}groupmode", "layer")
                    style = f"fill:{hex_color};fill-rule:evenodd;stroke:none"

                    _trace_potrace(binary, group, style, SVGNS)

                self.svg_string = etree.tostring(
                    root_svg, pretty_print=True, encoding="unicode"
                )
                self.root.after(0, show)
            except Exception as e:
                self.root.after(0, lambda e=e: messagebox.showerror("Vectorization failed", str(e)))

        def show():
            bar.stop()
            status_var.set("Vectorization complete.")
            preview = _svg_preview(self.svg_string) or self.clamped_image
            ph = self._photo(preview, max_size=600)
            ttk.Label(frame, image=ph).pack(pady=6)
            ttk.Button(frame, text="Check & Save →",
                       command=self.step9_thin_check).pack(pady=4)

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
            self.root.after(0, lambda: status_var.set(f"Thin check skipped: {e}"))
            try:
                with tempfile.NamedTemporaryFile(
                    suffix=".svg", delete=False, mode="w", encoding="utf-8"
                ) as f_in:
                    f_in.write(self.svg_string)
                    in_path = f_in.name
                out_path = in_path.replace(".svg", "_checked.svg")
                thin_process(in_path, out_path, min_mm, **CONFIG["thinchecker_params"])
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
            ph = self._photo(preview, max_size=600)
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
                        upscaled_image=self.upscaled_image,
                        color_layers=self.color_layers,
                        vectorized_svg=self.svg_string,
                    )
                    self._reset_session()

            self._nav_row(frame, back_cmd=self.step8_vectorize,
                          fwd_text="Save SVG…", fwd_cmd=save)

        threading.Thread(target=run, daemon=True).start()


# ── Module-level helpers ───────────────────────────────────────────────────────

def _trace_potrace(binary, group, style: str, SVGNS: str):
    """
    Trace a binary mask with potracer (pip install potracer) and append a single
    <path> element to *group*.  All subpaths (outer contours and holes) are
    accumulated into one d string so the SVG renderer can apply fill-rule:evenodd
    across them — Potrace already sets winding direction correctly for holes.
    binary is uint8, 255 = foreground.
    """
    from potrace import Bitmap   # package: potracer
    from PIL import Image
    from lxml import etree

    img  = Image.fromarray(binary)
    bm   = Bitmap(img, blacklevel=0.5)
    bm.invert()
    path = bm.trace(**CONFIG["potrace_params"])

    parts = []
    for curve in path:
        sp = curve.start_point
        d  = f"M {sp.x:.3f},{sp.y:.3f}"
        for seg in curve.segments:
            if seg.is_corner:
                c, e = seg.c, seg.end_point
                d += f" L {c.x:.3f},{c.y:.3f} L {e.x:.3f},{e.y:.3f}"
            else:
                c1, c2, e = seg.c1, seg.c2, seg.end_point
                d += (f" C {c1.x:.3f},{c1.y:.3f}"
                      f" {c2.x:.3f},{c2.y:.3f}"
                      f" {e.x:.3f},{e.y:.3f}")
        d += " Z"
        parts.append(d)

    if parts:
        el = etree.SubElement(group, f"{{{SVGNS}}}path")
        el.set("style", style)
        el.set("d", " ".join(parts))



def _svg_preview(svg_string: str, output_size: int = 800) -> Image.Image | None:
    """Rasterize SVG to a PIL Image for in-app preview. Returns None if unavailable."""
    try:
        import cairosvg
        png_bytes = cairosvg.svg2png(bytestring=svg_string.encode(), output_width=output_size)
        img = Image.open(io.BytesIO(png_bytes))
        img.load()
        return img
    except Exception:
        return None


def _save_svg_layers(svg_string: str, base_path: str,
                     width_mm: float, height_mm: float,
                     upscaled_image=None, color_layers=None,
                     vectorized_svg: str | None = None):
    """
    Save:
      • <name>.svg            — thin-checked layered SVG with mm dimensions
      • <name>_vectorized.svg — raw vectorized SVG before thin check
      • <name>_upscaled.png  — pre-clamped upscaled generation
      • <name>_<color>.png   — colored PNG per layer (color on background)
    """
    import numpy as np
    from lxml import etree

    base = Path(base_path)
    saved: list[str] = []

    # ── SVG ───────────────────────────────────────────────────────────────────
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
    saved.append(str(base))

    # ── Raw vectorized SVG (pre-thin-check) ───────────────────────────────────
    if vectorized_svg is not None:
        vec_path = base.with_name(f"{base.stem}_vectorized.svg")
        try:
            root_v = etree.fromstring(vectorized_svg.encode())
            vb_v = root_v.get("viewBox") or (
                f"0 0 {root_v.get('width', '100')} {root_v.get('height', '100')}"
            )
            root_v.set("width",   f"{width_mm}mm")
            root_v.set("height",  f"{height_mm}mm")
            root_v.set("viewBox", vb_v)
            vec_path.write_bytes(etree.tostring(root_v, pretty_print=True,
                                                xml_declaration=True, encoding="UTF-8"))
        except Exception:
            vec_path.write_text(vectorized_svg, encoding="utf-8")
        saved.append(str(vec_path))

    # ── Upscaled PNG ──────────────────────────────────────────────────────────
    if upscaled_image is not None:
        p = base.with_name(f"{base.stem}_upscaled.png")
        upscaled_image.save(p, format="PNG")
        saved.append(str(p))

    # ── Per-color PNGs (color pixels on background) ─────────────────────
    if color_layers:
        for name, rgb, mask_img in color_layers:
            safe = name.replace(" ", "_").replace("/", "_")
            p = base.with_name(f"{base.stem}_{safe}.png")
            # mask_img: 0=this color, 255=other — invert to make a colored layer
            arr = np.array(mask_img)           # 0 where color, 255 elsewhere
            out = np.full((*arr.shape, 3), 255, dtype=np.uint8)  # white canvas
            out[arr == 0] = list(rgb)          # paint actual color
            Image.fromarray(out, mode="RGB").save(p, format="PNG")
            saved.append(str(p))

    messagebox.showinfo("Saved",
                        f"Saved {len(saved)} file(s):\n" + "\n".join(saved))


def main():
    app = App()
    app.root.mainloop()


if __name__ == "__main__":
    main()
