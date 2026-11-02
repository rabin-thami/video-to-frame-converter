"""Tkinter desktop interface for Video Frame Extractor."""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .config import (
    IMAGE_FORMAT_LABELS,
    MODE_LABELS,
    MODE_SCENE,
    MODE_TIME_INTERVAL,
    RESIZE_CUSTOM,
    RESIZE_LABELS,
    RESIZE_MAX_HEIGHT,
    RESIZE_MAX_WIDTH,
    RESIZE_ORIGINAL,
    ContactSheetSettings,
    ExtractionSettings,
    ResizeSettings,
    SimilaritySettings,
    validate_settings,
)
from .errors import ValidationError
from .extractor import (
    ExtractionControl,
    ExtractionProgress,
    ExtractionResult,
    run_extraction,
)
from .probe import VideoInfo, estimate_candidate_frames, probe_video
from .utils import format_duration, format_timestamp, open_folder, parse_time

logger = logging.getLogger(__name__)

BG = "#f4f6fa"
CARD = "#ffffff"
MUTED = "#5f6b7a"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
GREEN = "#15803d"


class VideoFrameExtractorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Video Frame Extractor")
        self.root.geometry("1120x820")
        self.root.minsize(940, 680)
        self.root.configure(bg=BG)

        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._control: ExtractionControl | None = None
        self._running = False
        self._last_result: ExtractionResult | None = None
        self._video_info: VideoInfo | None = None
        self._default_output = str(Path.home() / "VideoFrames")
        self._pending_probe_path: Path | None = None

        self._configure_styles()
        self._create_variables()
        self._build_layout()
        self._update_method_fields()
        self._update_format_fields()
        self._update_resize_fields()
        self._update_contact_fields()
        self._update_filter_fields()
        self._update_threshold_label()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_queue)

    # ---------------------------------------------------------------- styling
    def _configure_styles(self) -> None:
        default_font = tkfont.nametofont("TkDefaultFont")
        default_font.configure(size=10)
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("App.TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("Bottom.TFrame", background=CARD)
        style.configure("TLabel", background=CARD, foreground="#1f2937")
        style.configure("App.TLabel", background=BG)
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED)
        style.configure("AppMuted.TLabel", background=BG, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, foreground="#111827", font=(default_font.actual("family"), 20, "bold"))
        style.configure("Subtitle.TLabel", background=BG, foreground=MUTED)
        style.configure("Section.TLabelframe", background=CARD)
        style.configure("Section.TLabelframe.Label", background=CARD, foreground="#111827", font=(default_font.actual("family"), 11, "bold"))
        style.configure("Card.TCheckbutton", background=CARD)
        style.configure("Card.TRadiobutton", background=CARD)
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", padding=(18, 8), borderwidth=0)
        style.map(
            "Accent.TButton",
            background=[("active", ACCENT_DARK), ("disabled", "#9db8f5")],
            foreground=[("disabled", "#f2f5ff")],
        )
        style.configure("TButton", padding=(10, 6))
        style.configure("Green.TButton", foreground=GREEN)
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#e3e8f0")

    def _create_variables(self) -> None:
        self.video_path_var = tk.StringVar()
        self.video_name_var = tk.StringVar(value="No video selected")
        self.video_path_label_var = tk.StringVar(value="Choose a video file to begin.")
        self.duration_var = tk.StringVar(value="—")
        self.resolution_var = tk.StringVar(value="—")
        self.fps_var = tk.StringVar(value="—")
        self.frames_var = tk.StringVar(value="—")
        self.candidates_var = tk.StringVar(value="—")
        self.source_var = tk.StringVar(value="—")

        self.method_var = tk.StringVar(value=MODE_LABELS[MODE_TIME_INTERVAL])
        self.interval_var = tk.StringVar(value="1.0")
        self.frame_interval_var = tk.StringVar(value="30")
        self.start_var = tk.StringVar(value="0")
        self.end_var = tk.StringVar(value="")

        self.output_var = tk.StringVar(value=self._default_output)
        self.format_var = tk.StringVar(value="PNG (lossless)")
        self.quality_var = tk.StringVar(value="95")
        self.resize_var = tk.StringVar(value=RESIZE_LABELS[RESIZE_ORIGINAL])
        self.resize_width_var = tk.StringVar(value="1280")
        self.resize_height_var = tk.StringVar(value="720")
        self.save_originals_var = tk.BooleanVar(value=False)

        self.filter_var = tk.BooleanVar(value=True)
        self.threshold_var = tk.DoubleVar(value=90.0)
        self.preserve_more_var = tk.BooleanVar(value=False)
        self.threshold_label_var = tk.StringVar(value="90%")
        self.effective_label_var = tk.StringVar(value="")

        self.contact_var = tk.BooleanVar(value=True)
        self.contact_columns_var = tk.StringVar(value="4")
        self.contact_max_var = tk.StringVar(value="40")
        self.metadata_var = tk.BooleanVar(value=True)

        self.progress_var = tk.DoubleVar(value=0.0)
        self.percent_var = tk.StringVar(value="0%")
        self.status_var = tk.StringVar(value="Ready. Select a video to start.")
        self.stats_var = tk.StringVar(value="Candidates: 0   ·   Retained: 0   ·   Removed: 0")
        self.timing_var = tk.StringVar(value="Elapsed: 00:00:00")

        for variable in (
            self.method_var,
            self.interval_var,
            self.frame_interval_var,
            self.start_var,
            self.end_var,
        ):
            variable.trace_add("write", lambda *_: self._update_candidate_estimate())
        self.format_var.trace_add("write", lambda *_: self._update_format_fields())
        self.resize_var.trace_add("write", lambda *_: self._update_resize_fields())
        self.filter_var.trace_add("write", lambda *_: self._update_filter_fields())
        self.preserve_more_var.trace_add("write", lambda *_: self._update_threshold_label())
        self.contact_var.trace_add("write", lambda *_: self._update_contact_fields())

    # ----------------------------------------------------------------- layout
    def _build_layout(self) -> None:
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        container = ttk.Frame(self.root, style="App.TFrame")
        container.grid(row=0, column=0, sticky="nsew")
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.inner = ttk.Frame(canvas, padding=(22, 16, 22, 18), style="App.TFrame")
        window = canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind(
            "<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.bind(
            "<Configure>", lambda event: canvas.itemconfigure(window, width=event.width)
        )

        def on_mousewheel(event: tk.Event) -> None:
            if event.num == 4:
                canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                canvas.yview_scroll(1, "units")
            elif event.delta:
                canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind_all("<MouseWheel>", on_mousewheel)
        canvas.bind_all("<Button-4>", on_mousewheel)
        canvas.bind_all("<Button-5>", on_mousewheel)

        self._build_header()
        self._build_video_section()
        self._build_settings_columns()
        self._build_bottom_bar()

    def _build_header(self) -> None:
        header = ttk.Frame(self.inner, style="App.TFrame")
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="Video Frame Extractor", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Extract distinct, representative images from a video — locally and privately.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))

    def _build_video_section(self) -> None:
        section = ttk.LabelFrame(self.inner, text=" 1 · Video ", padding=14, style="Section.TLabelframe")
        section.pack(fill="x", pady=(0, 12))
        section.grid_columnconfigure(1, weight=1)

        self.browse_button = ttk.Button(section, text="Browse Video…", command=self._browse_video)
        self.browse_button.grid(row=0, column=0, sticky="w")
        ttk.Label(
            section, textvariable=self.video_name_var, font=(tkfont.nametofont("TkDefaultFont").actual("family"), 11, "bold")
        ).grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Label(
            section,
            textvariable=self.video_path_label_var,
            style="Muted.TLabel",
            wraplength=920,
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 10))

        info = ttk.Frame(section, style="Card.TFrame")
        info.grid(row=2, column=0, columnspan=2, sticky="ew")
        for column in range(3):
            info.grid_columnconfigure(column, weight=1, uniform="info")
        cells = [
            ("Duration", self.duration_var),
            ("Resolution", self.resolution_var),
            ("Frame rate", self.fps_var),
            ("Total frames", self.frames_var),
            ("Est. candidate frames", self.candidates_var),
            ("Metadata source", self.source_var),
        ]
        for index, (title, variable) in enumerate(cells):
            row, column = divmod(index, 3)
            cell = ttk.Frame(info, style="Card.TFrame", padding=(0, 3))
            cell.grid(row=row, column=column, sticky="w")
            ttk.Label(cell, text=title, style="Muted.TLabel").pack(anchor="w")
            ttk.Label(cell, textvariable=variable).pack(anchor="w")

        ttk.Label(
            section,
            text=(
                "Candidate frames are the frames the extraction method will examine. "
                "The number of images kept after similarity filtering is determined by the "
                "video content and cannot be predicted reliably in advance."
            ),
            style="Muted.TLabel",
            wraplength=920,
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def _build_settings_columns(self) -> None:
        columns = ttk.Frame(self.inner, style="App.TFrame")
        columns.pack(fill="x")
        columns.grid_columnconfigure(0, weight=1, uniform="column")
        columns.grid_columnconfigure(1, weight=1, uniform="column")
        left = ttk.Frame(columns, style="App.TFrame")
        right = ttk.Frame(columns, style="App.TFrame")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        right.grid(row=0, column=1, sticky="nsew", padx=(7, 0))

        self._build_extraction_section(left)
        self._build_output_section(left)
        self._build_filter_section(right)
        self._build_review_section(right)

    def _build_extraction_section(self, parent: ttk.Frame) -> None:
        section = ttk.LabelFrame(parent, text=" 2 · Extraction ", padding=14, style="Section.TLabelframe")
        section.pack(fill="x", pady=(0, 12))
        section.grid_columnconfigure(1, weight=1)

        ttk.Label(section, text="Method").grid(row=0, column=0, sticky="w", pady=4)
        self.method_combo = ttk.Combobox(
            section,
            textvariable=self.method_var,
            values=list(MODE_LABELS.values()),
            state="readonly",
        )
        self.method_combo.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=4)
        self.method_combo.bind("<<ComboboxSelected>>", lambda _event: self._update_method_fields())

        ttk.Label(section, text="Time interval (seconds)").grid(row=1, column=0, sticky="w", pady=4)
        self.interval_entry = ttk.Entry(section, textvariable=self.interval_var, width=12)
        self.interval_entry.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=4)

        ttk.Label(section, text="Frame interval (every Nth)").grid(row=2, column=0, sticky="w", pady=4)
        self.frame_interval_entry = ttk.Entry(section, textvariable=self.frame_interval_var, width=12)
        self.frame_interval_entry.grid(row=2, column=1, sticky="ew", padx=(10, 0), pady=4)

        ttk.Label(section, text="Start time").grid(row=3, column=0, sticky="w", pady=4)
        self.start_entry = ttk.Entry(section, textvariable=self.start_var, width=12)
        self.start_entry.grid(row=3, column=1, sticky="ew", padx=(10, 0), pady=4)

        ttk.Label(section, text="End time (blank = end)").grid(row=4, column=0, sticky="w", pady=4)
        self.end_entry = ttk.Entry(section, textvariable=self.end_var, width=12)
        self.end_entry.grid(row=4, column=1, sticky="ew", padx=(10, 0), pady=4)

        ttk.Label(
            section,
            text="Times accept seconds (12.5) or HH:MM:SS (00:00:12.5).\n"
            "Every-frame extraction keeps all decodable frames unless filtering is enabled.",
            style="Muted.TLabel",
            wraplength=420,
            justify="left",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def _build_output_section(self, parent: ttk.Frame) -> None:
        section = ttk.LabelFrame(parent, text=" 4 · Output ", padding=14, style="Section.TLabelframe")
        section.pack(fill="x")
        section.grid_columnconfigure(1, weight=1)

        ttk.Label(section, text="Output folder").grid(row=0, column=0, sticky="w", pady=4)
        output_row = ttk.Frame(section, style="Card.TFrame")
        output_row.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=4)
        output_row.grid_columnconfigure(0, weight=1)
        ttk.Entry(output_row, textvariable=self.output_var).grid(row=0, column=0, sticky="ew")
        self.output_button = ttk.Button(output_row, text="Browse…", command=self._browse_output)
        self.output_button.grid(row=0, column=1, padx=(6, 0))

        ttk.Label(section, text="Image format").grid(row=1, column=0, sticky="w", pady=4)
        self.format_combo = ttk.Combobox(
            section,
            textvariable=self.format_var,
            values=list(IMAGE_FORMAT_LABELS),
            state="readonly",
        )
        self.format_combo.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=4)

        ttk.Label(section, text="Quality (JPEG / WebP)").grid(row=2, column=0, sticky="w", pady=4)
        self.quality_spin = ttk.Spinbox(
            section, from_=1, to=100, textvariable=self.quality_var, width=12
        )
        self.quality_spin.grid(row=2, column=1, sticky="ew", padx=(10, 0), pady=4)

        ttk.Label(section, text="Resize").grid(row=3, column=0, sticky="w", pady=4)
        self.resize_combo = ttk.Combobox(
            section,
            textvariable=self.resize_var,
            values=list(RESIZE_LABELS.values()),
            state="readonly",
        )
        self.resize_combo.grid(row=3, column=1, sticky="ew", padx=(10, 0), pady=4)

        ttk.Label(section, text="Width").grid(row=4, column=0, sticky="w", pady=4)
        self.width_entry = ttk.Entry(section, textvariable=self.resize_width_var, width=12)
        self.width_entry.grid(row=4, column=1, sticky="ew", padx=(10, 0), pady=4)

        ttk.Label(section, text="Height").grid(row=5, column=0, sticky="w", pady=4)
        self.height_entry = ttk.Entry(section, textvariable=self.resize_height_var, width=12)
        self.height_entry.grid(row=5, column=1, sticky="ew", padx=(10, 0), pady=4)

        self.save_originals_check = ttk.Checkbutton(
            section,
            text="Also save original-resolution copies",
            variable=self.save_originals_var,
            style="Card.TCheckbutton",
        )
        self.save_originals_check.grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Label(
            section,
            text="Aspect ratio is preserved and images are never enlarged. "
            "JPEG quality 95 is a good default for visual analysis.",
            style="Muted.TLabel",
            wraplength=420,
            justify="left",
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def _build_filter_section(self, parent: ttk.Frame) -> None:
        section = ttk.LabelFrame(
            parent, text=" 3 · Similarity filtering ", padding=14, style="Section.TLabelframe"
        )
        section.pack(fill="x", pady=(0, 12))
        section.grid_columnconfigure(0, weight=1)

        self.filter_check = ttk.Checkbutton(
            section,
            text="Keep only representative frames (remove duplicates)",
            variable=self.filter_var,
            style="Card.TCheckbutton",
        )
        self.filter_check.grid(row=0, column=0, sticky="w")

        threshold_row = ttk.Frame(section, style="Card.TFrame")
        threshold_row.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        threshold_row.grid_columnconfigure(0, weight=1)
        ttk.Label(threshold_row, text="Similarity threshold").grid(row=0, column=0, sticky="w")
        ttk.Label(threshold_row, textvariable=self.threshold_label_var).grid(row=0, column=1, sticky="e")
        self.threshold_scale = ttk.Scale(
            threshold_row,
            from_=50,
            to=100,
            variable=self.threshold_var,
            command=lambda _value: self._update_threshold_label(),
        )
        self.threshold_scale.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 0))

        ttk.Label(
            section,
            text=(
                "Higher similarity requirement keeps more frames; lower removes more. "
                "Frames with meaningful motion or a scene cut are kept even when they look similar."
            ),
            style="Muted.TLabel",
            wraplength=430,
            justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Label(
            section,
            textvariable=self.effective_label_var,
            style="Muted.TLabel",
        ).grid(row=3, column=0, sticky="w", pady=(2, 0))

        self.preserve_more_check = ttk.Checkbutton(
            section,
            text="Preserve more frames (stricter duplicate requirement)",
            variable=self.preserve_more_var,
            style="Card.TCheckbutton",
        )
        self.preserve_more_check.grid(row=4, column=0, sticky="w", pady=(8, 0))

        self.scene_note = ttk.Label(
            section,
            text="",
            style="Muted.TLabel",
            wraplength=430,
            justify="left",
        )
        self.scene_note.grid(row=5, column=0, sticky="w", pady=(6, 0))

        ttk.Label(
            section,
            text=(
                "Limitation: this is perceptual comparison, not semantic understanding. "
                "Very small changes (for example a subtle facial expression) can still be "
                "treated as redundant. Lower the threshold or disable filtering if needed."
            ),
            style="Muted.TLabel",
            wraplength=430,
            justify="left",
        ).grid(row=6, column=0, sticky="w", pady=(8, 0))

    def _build_review_section(self, parent: ttk.Frame) -> None:
        section = ttk.LabelFrame(
            parent, text=" 5 · Review & metadata ", padding=14, style="Section.TLabelframe"
        )
        section.pack(fill="x")
        section.grid_columnconfigure(1, weight=1)

        self.contact_check = ttk.Checkbutton(
            section,
            text="Generate contact sheet(s)",
            variable=self.contact_var,
            style="Card.TCheckbutton",
        )
        self.contact_check.grid(row=0, column=0, columnspan=2, sticky="w")

        ttk.Label(section, text="Columns").grid(row=1, column=0, sticky="w", pady=4)
        self.contact_columns_spin = ttk.Spinbox(
            section, from_=1, to=12, textvariable=self.contact_columns_var, width=12
        )
        self.contact_columns_spin.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=4)

        ttk.Label(section, text="Max thumbnails per sheet").grid(row=2, column=0, sticky="w", pady=4)
        self.contact_max_spin = ttk.Spinbox(
            section, from_=1, to=1000, textvariable=self.contact_max_var, width=12
        )
        self.contact_max_spin.grid(row=2, column=1, sticky="ew", padx=(10, 0), pady=4)

        self.metadata_check = ttk.Checkbutton(
            section,
            text="Write metadata.json report",
            variable=self.metadata_var,
            style="Card.TCheckbutton",
        )
        self.metadata_check.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Label(
            section,
            text=(
                "Contact sheets show the retained images with their frame numbers and "
                "original video timestamps. Large collections are split across several sheets."
            ),
            style="Muted.TLabel",
            wraplength=430,
            justify="left",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Label(
            section,
            text=(
                "Output layout: frames/ (retained images), originals/ (optional full-size "
                "copies), contact_sheets/, metadata.json and extraction.log."
            ),
            style="Muted.TLabel",
            wraplength=430,
            justify="left",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def _build_bottom_bar(self) -> None:
        bottom = ttk.Frame(self.root, padding=(22, 10, 22, 12), style="Bottom.TFrame")
        bottom.grid(row=1, column=0, sticky="ew")
        bottom.grid_columnconfigure(0, weight=1)

        progress_row = ttk.Frame(bottom, style="Bottom.TFrame")
        progress_row.grid(row=0, column=0, columnspan=2, sticky="ew")
        progress_row.grid_columnconfigure(0, weight=1)
        self.progress_bar = ttk.Progressbar(
            progress_row, variable=self.progress_var, maximum=100.0
        )
        self.progress_bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(
            progress_row, textvariable=self.percent_var, style="Muted.TLabel", width=6, anchor="e"
        ).grid(row=0, column=1, padx=(10, 0))

        ttk.Label(bottom, textvariable=self.status_var).grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Label(bottom, textvariable=self.timing_var, style="Muted.TLabel").grid(
            row=1, column=1, sticky="e", pady=(8, 0)
        )
        ttk.Label(bottom, textvariable=self.stats_var, style="Muted.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(3, 8)
        )

        buttons = ttk.Frame(bottom, style="Bottom.TFrame")
        buttons.grid(row=3, column=0, columnspan=2, sticky="ew")
        self.start_button = ttk.Button(
            buttons, text="Start Extraction", style="Accent.TButton", command=self._start_extraction
        )
        self.start_button.pack(side="left")
        self.pause_button = ttk.Button(buttons, text="Pause", command=self._pause, state="disabled")
        self.pause_button.pack(side="left", padx=(8, 0))
        self.resume_button = ttk.Button(
            buttons, text="Resume", command=self._resume, state="disabled"
        )
        self.resume_button.pack(side="left", padx=(8, 0))
        self.cancel_button = ttk.Button(
            buttons, text="Cancel", command=self._cancel, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=(8, 0))
        self.open_button = ttk.Button(
            buttons, text="Open Output Folder", command=self._open_output, state="disabled"
        )
        self.open_button.pack(side="right")

    # -------------------------------------------------------------- selection
    def _browse_video(self) -> None:
        path = filedialog.askopenfilename(
            title="Select a video",
            filetypes=[
                (
                    "Video files",
                    "*.mp4 *.mov *.avi *.mkv *.webm *.wmv *.mpeg *.mpg",
                ),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return
        video_path = Path(path)
        self.video_path_var.set(str(video_path))
        self.video_name_var.set(video_path.name)
        self.video_path_label_var.set(str(video_path))
        self._video_info = None
        self._reset_info_labels()
        self._last_result = None
        self.open_button.state(["disabled"])
        self.candidates_var.set("Reading…")
        self.status_var.set("Reading video metadata…")
        current_output = self.output_var.get().strip()
        if not current_output or current_output == self._default_output:
            self.output_var.set(str(video_path.parent / "ExtractedFrames"))
        self._pending_probe_path = video_path
        threading.Thread(target=self._probe_worker, args=(video_path,), daemon=True).start()

    def _probe_worker(self, path: Path) -> None:
        try:
            info = probe_video(path)
            self._queue.put(("probe", path, info, None))
        except Exception as exc:
            logger.exception("Metadata probe failed")
            self._queue.put(("probe", path, None, str(exc)))

    def _reset_info_labels(self) -> None:
        for variable in (
            self.duration_var,
            self.resolution_var,
            self.fps_var,
            self.frames_var,
            self.candidates_var,
            self.source_var,
        ):
            variable.set("—")

    def _apply_probe_result(self, path: Path, info: VideoInfo | None, error: str | None) -> None:
        if self._pending_probe_path != path:
            return
        self._pending_probe_path = None
        if info is None:
            self._video_info = None
            self._reset_info_labels()
            self.status_var.set("Could not read this video.")
            messagebox.showerror(
                "Cannot read video",
                error or "The selected file could not be read as a video.",
            )
            return
        self._video_info = info
        self.duration_var.set(
            f"{format_duration(info.duration)} ({info.duration:.1f} s)"
            if info.duration
            else "Unknown"
        )
        self.resolution_var.set(info.resolution_label)
        self.fps_var.set(info.fps_label)
        self.frames_var.set(info.frame_count_label)
        self.source_var.set(info.source)
        self.status_var.set("Ready to extract.")
        self._update_candidate_estimate()

    def _browse_output(self) -> None:
        folder = filedialog.askdirectory(title="Choose output folder")
        if folder:
            self.output_var.set(folder)

    # --------------------------------------------------------------- toggles
    def _selected_mode(self) -> str:
        label = self.method_var.get()
        for mode, mode_label in MODE_LABELS.items():
            if mode_label == label:
                return mode
        return MODE_TIME_INTERVAL

    def _selected_format(self) -> str:
        return IMAGE_FORMAT_LABELS.get(self.format_var.get(), "png")

    def _selected_resize_mode(self) -> str:
        for mode, label in RESIZE_LABELS.items():
            if label == self.resize_var.get():
                return mode
        return RESIZE_ORIGINAL

    @staticmethod
    def _set_enabled(widget: ttk.Widget, enabled: bool) -> None:
        widget.state(["!disabled"] if enabled else ["disabled"])

    def _update_method_fields(self) -> None:
        mode = self._selected_mode()
        self._set_enabled(self.interval_entry, mode == "time_interval")
        self._set_enabled(self.frame_interval_entry, mode == "frame_interval")
        if mode == MODE_SCENE:
            self.filter_var.set(True)
            self.filter_check.state(["disabled"])
            self.scene_note.configure(
                text="Scene-aware mode always uses similarity filtering. It evaluates every "
                "frame and keeps representative frames at scene changes and meaningful motion."
            )
        else:
            self.filter_check.state(["!disabled"])
            self.scene_note.configure(text="")
        self._update_filter_fields()
        self._update_candidate_estimate()

    def _update_format_fields(self) -> None:
        image_format = self._selected_format()
        self._set_enabled(self.quality_spin, image_format != "png")
        if image_format == "png":
            self.quality_var.set("95")

    def _update_resize_fields(self) -> None:
        mode = self._selected_resize_mode()
        self._set_enabled(self.width_entry, mode in (RESIZE_MAX_WIDTH, RESIZE_CUSTOM))
        self._set_enabled(self.height_entry, mode in (RESIZE_MAX_HEIGHT, RESIZE_CUSTOM))
        self._set_enabled(self.save_originals_check, mode != RESIZE_ORIGINAL)
        if mode == RESIZE_ORIGINAL:
            self.save_originals_var.set(False)

    def _update_filter_fields(self) -> None:
        enabled = bool(self.filter_var.get())
        self._set_enabled(self.threshold_scale, enabled)
        self._set_enabled(self.preserve_more_check, enabled)
        self._update_threshold_label()

    def _update_contact_fields(self) -> None:
        enabled = bool(self.contact_var.get())
        self._set_enabled(self.contact_columns_spin, enabled)
        self._set_enabled(self.contact_max_spin, enabled)

    def _update_threshold_label(self) -> None:
        value = round(self.threshold_var.get())
        self.threshold_label_var.set(f"{value}%")
        if self.preserve_more_var.get():
            effective = min(100, value + 5)
            self.effective_label_var.set(
                f"Effective threshold: {effective}% · motion guard more sensitive"
            )
        else:
            self.effective_label_var.set("")

    def _update_candidate_estimate(self) -> None:
        if self._video_info is None:
            return
        try:
            mode = self._selected_mode()
            interval = float(self.interval_var.get())
            frame_interval = int(self.frame_interval_var.get())
            start = parse_time(self.start_var.get() or "0", "start time")
            end = parse_time(self.end_var.get(), "end time") if self.end_var.get().strip() else None
        except (ValueError, ValidationError):
            self.candidates_var.set("Check settings")
            return
        estimate = estimate_candidate_frames(
            self._video_info, mode, interval, frame_interval, start, end
        )
        if estimate is None:
            self.candidates_var.set("Unknown")
        elif mode == MODE_SCENE:
            self.candidates_var.set(f"≈ {estimate:,} (all frames examined)")
        else:
            self.candidates_var.set(f"≈ {estimate:,} (estimated)")
        self._update_threshold_label()

    # ------------------------------------------------------------- extraction
    def _collect_settings(self) -> ExtractionSettings:
        video_text = self.video_path_var.get().strip()
        if not video_text:
            raise ValidationError("Please select a video file first.")
        output_text = self.output_var.get().strip()
        if not output_text:
            raise ValidationError("Please choose an output folder.")
        try:
            interval = float(self.interval_var.get())
        except ValueError as exc:
            raise ValidationError("The time interval must be a number, for example 1.0.") from exc
        try:
            frame_interval = int(self.frame_interval_var.get())
        except ValueError as exc:
            raise ValidationError("The frame interval must be a whole number, for example 30.") from exc
        try:
            quality = int(self.quality_var.get())
        except ValueError as exc:
            raise ValidationError("Image quality must be a whole number between 1 and 100.") from exc
        try:
            columns = int(self.contact_columns_var.get())
            max_thumbnails = int(self.contact_max_var.get())
        except ValueError as exc:
            raise ValidationError("Contact sheet columns and thumbnail limits must be whole numbers.") from exc

        start = parse_time(self.start_var.get() or "0", "start time")
        end = parse_time(self.end_var.get(), "end time") if self.end_var.get().strip() else None

        resize_mode = self._selected_resize_mode()
        width: int | None = None
        height: int | None = None
        if resize_mode in (RESIZE_MAX_WIDTH, RESIZE_CUSTOM):
            try:
                width = int(self.resize_width_var.get())
            except ValueError as exc:
                raise ValidationError("Please enter a whole-number width.") from exc
        if resize_mode in (RESIZE_MAX_HEIGHT, RESIZE_CUSTOM):
            try:
                height = int(self.resize_height_var.get())
            except ValueError as exc:
                raise ValidationError("Please enter a whole-number height.") from exc

        settings = ExtractionSettings(
            video_path=Path(video_text).expanduser(),
            output_root=Path(output_text).expanduser(),
            mode=self._selected_mode(),
            interval_seconds=interval,
            frame_interval=frame_interval,
            start_time=start,
            end_time=end,
            image_format=self._selected_format(),
            quality=quality,
            similarity=SimilaritySettings(
                enabled=bool(self.filter_var.get()),
                threshold=float(self.threshold_var.get()),
                preserve_more=bool(self.preserve_more_var.get()),
            ),
            resize=ResizeSettings(
                mode=resize_mode,
                width=width,
                height=height,
                save_originals=bool(self.save_originals_var.get()),
            ),
            contact_sheet=ContactSheetSettings(
                enabled=bool(self.contact_var.get()),
                columns=columns,
                max_thumbnails=max_thumbnails,
            ),
            write_metadata=bool(self.metadata_var.get()),
        )
        validate_settings(settings)
        return settings

    def _start_extraction(self) -> None:
        if self._running:
            return
        try:
            settings = self._collect_settings()
        except ValidationError as exc:
            messagebox.showerror("Check your settings", str(exc))
            return
        except Exception as exc:
            logger.exception("Could not start extraction")
            messagebox.showerror("Cannot start", str(exc))
            return

        if self._video_info and self._video_info.duration and self._video_info.duration > 1800:
            if not messagebox.askyesno(
                "Long video",
                f"This video is {format_duration(self._video_info.duration)} long. "
                "Extraction can take a long time and use a lot of disk space. Continue?",
            ):
                return
        elif self._video_info:
            estimate = estimate_candidate_frames(
                self._video_info,
                settings.mode,
                settings.interval_seconds,
                settings.frame_interval,
                settings.start_time,
                settings.end_time,
            )
            if estimate and estimate > 5000 and not messagebox.askyesno(
                "Many frames",
                f"About {estimate:,} candidate frames will be examined. "
                "This can take a long time and use a lot of disk space. Continue?",
            ):
                return

        self._last_result = None
        self.open_button.state(["disabled"])
        self.progress_var.set(0)
        self.percent_var.set("0%")
        self.stats_var.set("Candidates: 0   ·   Retained: 0   ·   Removed: 0")
        self.timing_var.set("Elapsed: 00:00:00")
        self.status_var.set("Starting extraction…")
        self._control = ExtractionControl()
        self._set_running(True)

        def worker() -> None:
            try:
                result = run_extraction(
                    settings,
                    control=self._control,
                    progress_callback=lambda progress: self._queue.put(("progress", progress)),
                )
                self._queue.put(("done", result))
            except Exception as exc:
                logger.exception("Extraction failed")
                self._queue.put(("error", str(exc)))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def _set_running(self, running: bool) -> None:
        self._running = running
        self._set_enabled(self.start_button, not running)
        self._set_enabled(self.browse_button, not running)
        self._set_enabled(self.output_button, not running)
        self._set_enabled(self.cancel_button, running)
        if running:
            self._set_enabled(self.pause_button, True)
            self._set_enabled(self.resume_button, False)
        else:
            self._set_enabled(self.pause_button, False)
            self._set_enabled(self.resume_button, False)

    def _pause(self) -> None:
        if self._control is not None and self._running:
            self._control.pause()
            self._set_enabled(self.pause_button, False)
            self._set_enabled(self.resume_button, True)
            self.status_var.set("Paused. Click Resume to continue.")

    def _resume(self) -> None:
        if self._control is not None and self._running:
            self._control.resume()
            self._set_enabled(self.pause_button, True)
            self._set_enabled(self.resume_button, False)
            self.status_var.set("Resuming…")

    def _cancel(self) -> None:
        if self._control is not None and self._running:
            if not messagebox.askyesno(
                "Cancel extraction",
                "Cancel processing? Images already saved will be kept.",
            ):
                return
            self._control.cancel()
            self._set_enabled(self.cancel_button, False)
            self._set_enabled(self.pause_button, False)
            self._set_enabled(self.resume_button, False)
            self.status_var.set("Cancelling…")

    def _open_output(self) -> None:
        if self._last_result is not None and self._last_result.job_dir.exists():
            try:
                open_folder(self._last_result.job_dir)
            except OSError as exc:
                messagebox.showerror("Cannot open folder", str(exc))

    # ------------------------------------------------------------- queue loop
    def _drain_queue(self) -> None:
        try:
            while True:
                message = self._queue.get_nowait()
                kind = message[0]
                if kind == "probe":
                    self._apply_probe_result(message[1], message[2], message[3])
                elif kind == "progress":
                    self._apply_progress(message[1])
                elif kind == "done":
                    self._finish(message[1])
                elif kind == "error":
                    self._fail(message[1])
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._drain_queue)

    def _apply_progress(self, progress: ExtractionProgress) -> None:
        if progress.fraction is not None:
            percent = progress.fraction * 100.0
            self.progress_var.set(percent)
            self.percent_var.set(f"{percent:.0f}%")
        else:
            self.percent_var.set("—")
        self.status_var.set(progress.status + "…")
        self.stats_var.set(
            f"Candidates: {progress.candidates:,}   ·   "
            f"Retained: {progress.retained:,}   ·   "
            f"Removed: {progress.removed:,}"
        )
        eta = format_timestamp(progress.eta_seconds, milliseconds=False) if progress.eta_seconds is not None else "—"
        self.timing_var.set(
            f"Elapsed: {format_timestamp(progress.elapsed_seconds, milliseconds=False)}   ·   ETA: {eta}"
        )

    def _finish(self, result: ExtractionResult) -> None:
        self._running = False
        self._set_running(False)
        self._last_result = result
        self.progress_var.set(100.0 if not result.cancelled else self.progress_var.get())
        self.percent_var.set("100%" if not result.cancelled else "—")
        self.stats_var.set(
            f"Candidates: {result.candidate_frames:,}   ·   "
            f"Retained: {result.retained_frames:,}   ·   "
            f"Removed: {result.removed_frames:,}"
        )
        self.timing_var.set(
            f"Elapsed: {format_timestamp(result.elapsed_seconds, milliseconds=False)}"
        )
        self.status_var.set(
            "Extraction cancelled. Saved images were kept."
            if result.cancelled
            else "Extraction complete."
        )
        self._set_enabled(self.open_button, result.job_dir.exists())

        summary = (
            f"Candidate frames: {result.candidate_frames:,}\n"
            f"Images retained: {result.retained_frames:,}\n"
            f"Frames removed as redundant: {result.removed_frames:,}\n"
            f"Contact sheets: {len(result.contact_sheets)}\n"
            f"Elapsed: {format_timestamp(result.elapsed_seconds, milliseconds=False)}\n\n"
            f"Output folder:\n{result.job_dir}"
        )
        if result.errors:
            details = "\n".join(f"• {error}" for error in result.errors[:5])
            if len(result.errors) > 5:
                details += f"\n… and {len(result.errors) - 5} more (see extraction.log)."
            messagebox.showwarning(
                "Extraction finished with issues",
                f"{summary}\n\nIssues:\n{details}",
            )
        elif result.cancelled:
            messagebox.showinfo("Extraction cancelled", summary)
        elif result.retained_frames == 0:
            messagebox.showwarning(
                "No images saved",
                f"{summary}\n\nNo representative frames were written. "
                "Check the video and extraction range.",
            )
        else:
            messagebox.showinfo("Extraction complete", summary)

    def _fail(self, message: str) -> None:
        self._running = False
        self._set_running(False)
        self.status_var.set("Extraction failed.")
        self.percent_var.set("—")
        messagebox.showerror("Extraction failed", message)

    # ----------------------------------------------------------------- closing
    def _on_close(self) -> None:
        if self._running and self._control is not None:
            if not messagebox.askyesno(
                "Quit while extracting?",
                "Extraction is still running. Cancel it and quit?",
            ):
                return
            self._control.cancel()
        self.root.destroy()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    root = tk.Tk()
    VideoFrameExtractorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
