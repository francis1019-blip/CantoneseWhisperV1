"""
廣東話字幕轉錄工具 (類似 Whisper Desktop，但可自訂設定)

安裝 (只需一次):
    pip install faster-whisper opencc-python-reimplemented

執行:
    python cantonese_whisper_gui.py

特點:
  - 離線運行 (模型下載後，音訊不會上傳)
  - 可揀模型 (內置名稱 / 本機 CTranslate2 模型資料夾 / Hugging Face repo)
  - 可設定每行最多字數，優先喺標點位斷行
  - 可轉繁體 (香港字形)、移除標點、自訂提示詞
  - 設定自動儲存喺 settings.json
"""
import json
import math
import os
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import sys

_BASE = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(_BASE, "settings.json")

DEFAULTS = {
    "model": "large-v3",
    "language": "yue",
    "device": "auto",
    "max_chars": 16,
    "theme": "light",
    "strip_punct": True,
    "to_traditional": True,
    "vad": True,
    "beam_size": 5,
    "prompt": "以下係香港廣東話新聞，請用繁體中文書寫。",
    "save_txt": False,
    "last_dir": "",
}

STRONG = set("，。？！、；：,.?!;:…")          # 優先斷行位
SENT_END = set("。？！.?!")                    # 句號位，唔會同下一句合併
STRIP = STRONG | set("「」『』“”‘’()（）《》〈〉【】[]—～~")

MODEL_CHOICES = ["large-v3", "large-v3-turbo", "medium", "small", "base"]
THEMES = {
    "light": dict(
        bg="#f5f7fa", surface="#ffffff", fg="#17212b", field="#ffffff",
        btn="#eef2f6", sel="#d9edf5", accent="#147d9a", line="#d9e1e8", muted="#7a8793"
    ),
    "dark": dict(
        bg="#11161b", surface="#1a2128", fg="#e8eef3", field="#202930",
        btn="#27313a", sel="#315d70", accent="#63c1df", line="#39454f", muted="#8b98a4"
    ),
}
THEME_LABELS = {"淺色": "light", "深色": "dark"}
LANG_CHOICES = {"廣東話 (yue)": "yue", "中文 (zh)": "zh", "自動偵測": None, "英文 (en)": "en"}


# ---------------------------------------------------------------- 字幕斷行
def is_ascii_alnum(c):
    return c.isascii() and c.isalnum()


def visible(ch):
    c = ch["c"]
    return not c.isspace() and c not in STRIP


def count_vis(items):
    return sum(1 for x in items if visible(x))


def hard_split(items, max_chars):
    """一個子句太長 → 平均分成幾行，盡量唔切開英文/數字。"""
    n = count_vis(items)
    parts = math.ceil(n / max_chars)
    target = math.ceil(n / parts)
    lines, cur, seen = [], [], 0
    for i, it in enumerate(items):
        cur.append(it)
        if visible(it):
            seen += 1
        if seen >= target and len(lines) < parts - 1:
            nxt = items[i + 1] if i + 1 < len(items) else None
            # 避免切開英文字 / 數字
            if nxt and is_ascii_alnum(it["c"]) and is_ascii_alnum(nxt["c"]):
                continue
            lines.append(cur)
            cur, seen = [], 0
    if cur:
        lines.append(cur)
    return lines


def split_lines(chars, max_chars):
    clauses, cur = [], []
    for ch in chars:
        cur.append(ch)
        if ch["c"] in STRONG:
            clauses.append(cur)
            cur = []
    if cur:
        clauses.append(cur)

    lines, buf = [], []

    def flush():
        nonlocal buf
        if buf:
            lines.append(buf)
        buf = []

    for cl in clauses:
        n = count_vis(cl)
        if n == 0:
            buf += cl
            continue
        if n > max_chars:
            flush()
            lines.extend(hard_split(cl, max_chars))
            continue
        can_merge = buf and buf[-1]["c"] not in SENT_END and count_vis(buf) + n <= max_chars
        if can_merge:
            buf += cl
        else:
            flush()
            buf = list(cl)
    flush()
    return lines


def tidy(text, strip_punct):
    if strip_punct:
        text = "".join(c for c in text if c not in STRIP)
    # 中文字之間唔要空格
    text = re.sub(r"(?<=[^\x00-\x7f])\s+|\s+(?=[^\x00-\x7f])", "", text)
    return text.strip()


def fmt_time(t):
    t = max(0.0, t)
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def segment_to_chars(seg, converter):
    out = []
    words = getattr(seg, "words", None)
    if words:
        for w in words:
            txt = converter(w.word) if converter else w.word
            if not txt:
                continue
            d = (w.end - w.start) / len(txt)
            for i, c in enumerate(txt):
                out.append({"c": c, "s": w.start + i * d, "e": w.start + (i + 1) * d})
    else:
        txt = converter(seg.text) if converter else seg.text
        if txt:
            d = (seg.end - seg.start) / len(txt)
            for i, c in enumerate(txt):
                out.append({"c": c, "s": seg.start + i * d, "e": seg.start + (i + 1) * d})
    return out


# ---------------------------------------------------------------- 主程式
class App:
    def __init__(self, root):
        self.root = root
        root.title("廣東話字幕轉錄工具")
        self.cfg = dict(DEFAULTS)
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                self.cfg.update(json.load(f))
        except Exception:
            pass
        self.files = []
        self.q = queue.Queue()
        self.model = None
        self.model_key = None
        self.running = False
        self.build_ui()
        self.root.after(150, self.poll)

    def build_ui(self):
        # 現代化卡片式介面；保留原有所有 transcription 功能。
        self.root.minsize(820, 680)
        self.root.geometry("940x760")

        self.ui = {}
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        outer = tk.Frame(self.root, bd=0, highlightthickness=0)
        outer.grid(row=0, column=0, sticky="nsew", padx=18, pady=18)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        # Header
        header = tk.Frame(outer, bd=0)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        header.columnconfigure(0, weight=1)

        title_box = tk.Frame(header, bd=0)
        title_box.grid(row=0, column=0, sticky="w")
        tk.Label(title_box, text="廣東話字幕轉錄",
                 font=("Segoe UI", 20, "bold"), anchor="w").pack(anchor="w")
        tk.Label(title_box, text="離線語音轉文字・SRT 字幕工具",
                 font=("Segoe UI", 10), anchor="w").pack(anchor="w", pady=(2, 0))

        theme_box = tk.Frame(header, bd=0)
        theme_box.grid(row=0, column=1, sticky="e")
        tk.Label(theme_box, text="外觀", font=("Segoe UI", 9)).pack(side="left", padx=(0, 7))
        self.v_theme = tk.StringVar(
            value="深色" if self.cfg.get("theme") == "dark" else "淺色"
        )
        self.theme_cb = ttk.Combobox(
            theme_box, textvariable=self.v_theme,
            values=list(THEME_LABELS), state="readonly", width=6
        )
        self.theme_cb.pack(side="left")
        self.theme_cb.bind("<<ComboboxSelected>>", self.on_theme_change)

        # File card
        file_card = tk.Frame(outer, bd=0, highlightthickness=1)
        file_card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        file_card.columnconfigure(1, weight=1)

        tk.Label(file_card, text="①  選擇音訊 / 影片",
                 font=("Segoe UI", 11, "bold")).grid(
                     row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(13, 8)
                 )

        self.file_btn = ttk.Button(file_card, text="選擇檔案…", command=self.pick_files)
        self.file_btn.grid(row=1, column=0, sticky="w", padx=(16, 8), pady=(0, 14))

        self.lbl_files = tk.Label(file_card, text="尚未選擇檔案", anchor="w")
        self.lbl_files.grid(row=1, column=1, sticky="ew", padx=8, pady=(0, 14))

        # Settings card
        settings_card = tk.Frame(outer, bd=0, highlightthickness=1)
        settings_card.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        settings_card.columnconfigure(1, weight=1)
        settings_card.columnconfigure(3, weight=1)

        tk.Label(settings_card, text="②  轉錄設定",
                 font=("Segoe UI", 11, "bold")).grid(
                     row=0, column=0, columnspan=4, sticky="w", padx=16, pady=(13, 8)
                 )

        self.v_model = tk.StringVar(value=self.cfg["model"])
        self.v_lang = tk.StringVar(
            value=next((k for k, v in LANG_CHOICES.items()
                        if v == self.cfg["language"]), "廣東話 (yue)")
        )
        self.v_dev = tk.StringVar(value=self.cfg["device"])
        self.v_max = tk.IntVar(value=self.cfg["max_chars"])
        self.v_beam = tk.IntVar(value=self.cfg["beam_size"])
        self.v_prompt = tk.StringVar(value=self.cfg["prompt"])

        def label(row, col, txt):
            tk.Label(settings_card, text=txt, anchor="w",
                     font=("Segoe UI", 9)).grid(
                         row=row, column=col, sticky="w", padx=(16 if col == 0 else 10),
                         pady=6
                     )

        label(1, 0, "模型")
        self.model_cb = ttk.Combobox(
            settings_card, textvariable=self.v_model, values=MODEL_CHOICES
        )
        self.model_cb.grid(row=1, column=1, sticky="ew", padx=8, pady=5)
        self.model_dir_btn = ttk.Button(
            settings_card, text="本機資料夾…", command=self.pick_model_dir
        )
        self.model_dir_btn.grid(row=1, column=2, sticky="w", padx=8, pady=5)

        label(1, 3, "語言")
        self.lang_cb = ttk.Combobox(
            settings_card, textvariable=self.v_lang,
            values=list(LANG_CHOICES), state="readonly"
        )
        self.lang_cb.grid(row=1, column=4, sticky="ew", padx=(8, 16), pady=5)

        label(2, 0, "運算裝置")
        self.dev_cb = ttk.Combobox(
            settings_card, textvariable=self.v_dev,
            values=["auto", "cpu", "cuda"], state="readonly"
        )
        self.dev_cb.grid(row=2, column=1, sticky="ew", padx=8, pady=5)

        label(2, 3, "每行最多字數")
        self.max_spin = ttk.Spinbox(
            settings_card, from_=4, to=60, textvariable=self.v_max, width=7
        )
        self.max_spin.grid(row=2, column=4, sticky="w", padx=(8, 16), pady=5)

        label(3, 0, "Beam size")
        self.beam_spin = ttk.Spinbox(
            settings_card, from_=1, to=10, textvariable=self.v_beam, width=7
        )
        self.beam_spin.grid(row=3, column=1, sticky="w", padx=8, pady=5)

        label(3, 3, "提示詞")
        self.prompt_entry = ttk.Entry(settings_card, textvariable=self.v_prompt)
        self.prompt_entry.grid(row=3, column=4, sticky="ew", padx=(8, 16), pady=5)

        self.v_strip = tk.BooleanVar(value=self.cfg["strip_punct"])
        self.v_trad = tk.BooleanVar(value=self.cfg["to_traditional"])
        self.v_vad = tk.BooleanVar(value=self.cfg["vad"])
        self.v_txt = tk.BooleanVar(value=self.cfg["save_txt"])

        opts = tk.Frame(settings_card, bd=0)
        opts.grid(row=4, column=0, columnspan=5, sticky="w", padx=12, pady=(6, 13))
        for txt, var in [
            ("移除標點", self.v_strip),
            ("轉繁體(香港)", self.v_trad),
            ("VAD 靜音過濾", self.v_vad),
            ("同時輸出 TXT", self.v_txt),
        ]:
            ttk.Checkbutton(opts, text=txt, variable=var).pack(
                side="left", padx=5
            )

        # Output / log card
        output_card = tk.Frame(outer, bd=0, highlightthickness=1)
        output_card.grid(row=3, column=0, sticky="nsew")
        output_card.columnconfigure(0, weight=1)
        output_card.rowconfigure(2, weight=1)

        tk.Label(output_card, text="③  轉錄進度",
                 font=("Segoe UI", 11, "bold")).grid(
                     row=0, column=0, sticky="w", padx=16, pady=(13, 8)
                 )

        action = tk.Frame(output_card, bd=0)
        action.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))
        action.columnconfigure(1, weight=1)

        self.btn = ttk.Button(action, text="▶  開始轉錄", command=self.start)
        self.btn.grid(row=0, column=0, sticky="w", padx=(0, 12))

        self.bar = ttk.Progressbar(
            action, mode="determinate", maximum=100, style="Accent.Horizontal.TProgressbar"
        )
        self.bar.grid(row=0, column=1, sticky="ew", pady=4)

        log_frame = tk.Frame(output_card, bd=0)
        log_frame.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 16))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log = tk.Text(
            log_frame, height=12, width=80, state="disabled",
            font=("Consolas", 9), bd=0, padx=10, pady=8,
            wrap="word"
        )
        self.log.grid(row=0, column=0, sticky="nsew")

        self.apply_theme(self.cfg.get("theme", "light"))

    # ---- 外觀 (淺色 / 深色)
    def on_theme_change(self, _event=None):
        self.apply_theme(THEME_LABELS[self.v_theme.get()])
        self.save_settings()

    def apply_theme(self, name):
        p = THEMES.get(name, THEMES["light"])
        st = ttk.Style(self.root)
        st.theme_use("clam")

        self.root.configure(bg=p["bg"])

        # ttk
        st.configure(".", background=p["surface"], foreground=p["fg"])
        st.configure("TButton", background=p["btn"], foreground=p["fg"],
                     padding=(13, 7), bordercolor=p["line"], relief="flat")
        st.map("TButton",
               background=[("active", p["sel"]), ("disabled", p["bg"])],
               foreground=[("disabled", p["muted"])])

        st.configure("TCheckbutton", background=p["surface"], foreground=p["fg"])
        st.map("TCheckbutton", background=[("active", p["surface"])],
               indicatorcolor=[("selected", p["accent"]), ("!selected", p["field"])])

        st.configure("TEntry", fieldbackground=p["field"], foreground=p["fg"],
                     insertcolor=p["fg"], bordercolor=p["line"])
        st.configure("TCombobox", fieldbackground=p["field"], background=p["btn"],
                     foreground=p["fg"], arrowcolor=p["fg"], bordercolor=p["line"])
        st.map("TCombobox",
               fieldbackground=[("readonly", p["field"])],
               foreground=[("readonly", p["fg"])],
               selectbackground=[("readonly", p["field"])],
               selectforeground=[("readonly", p["fg"])])

        st.configure("TSpinbox", fieldbackground=p["field"], background=p["btn"],
                     foreground=p["fg"], arrowcolor=p["fg"],
                     insertcolor=p["fg"], bordercolor=p["line"])
        st.configure("Accent.Horizontal.TProgressbar",
                     background=p["accent"], troughcolor=p["field"],
                     bordercolor=p["line"])

        # 卡片與 Tk widgets
        def recolor(widget):
            try:
                if isinstance(widget, tk.Label):
                    widget.configure(bg=p["surface"], fg=p["fg"])
                elif isinstance(widget, tk.Frame):
                    widget.configure(bg=p["surface"])
                elif isinstance(widget, tk.Text):
                    widget.configure(
                        bg=p["field"], fg=p["fg"], insertbackground=p["fg"],
                        selectbackground=p["sel"], highlightbackground=p["line"],
                        highlightcolor=p["accent"]
                    )
            except tk.TclError:
                pass
            for child in widget.winfo_children():
                recolor(child)

        for child in self.root.winfo_children():
            recolor(child)

        # 根容器本身保持主背景
        for widget in [getattr(self, "ui", None)]:
            pass

        # 所有卡片 Frame 使用 surface，外圍保持 bg
        # 針對已建立的主要容器重新設定邊框色
        def card_border(widget):
            if isinstance(widget, tk.Frame) and widget is not self.root:
                try:
                    if widget.cget("highlightthickness") == 1:
                        widget.configure(highlightbackground=p["line"])
                except tk.TclError:
                    pass
            for c in widget.winfo_children():
                card_border(c)
        card_border(self.root)

        self.log.configure(
            bg=p["field"], fg=p["fg"], insertbackground=p["fg"],
            selectbackground=p["sel"], highlightthickness=1,
            highlightbackground=p["line"], relief="flat"
        )

        # 下拉選單清單
        self.root.option_add("*TCombobox*Listbox.background", p["field"])
        self.root.option_add("*TCombobox*Listbox.foreground", p["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", p["sel"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", p["fg"])

        for cb in self._find(self.root, ttk.Combobox):
            try:
                lb = f"{cb}.popdown.f.l"
                for opt, val in (
                    ("-background", p["field"]),
                    ("-foreground", p["fg"]),
                    ("-selectbackground", p["sel"]),
                    ("-selectforeground", p["fg"]),
                ):
                    self.root.tk.call(lb, "configure", opt, val)
            except Exception:
                pass

    def _find(self, w, cls):
        out = []
        for c in w.winfo_children():
            if isinstance(c, cls):
                out.append(c)
            out += self._find(c, cls)
        return out

    # ---- helpers
    def say(self, msg):
        self.q.put(("log", msg))

    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.log.config(state="normal")
                    self.log.insert("end", val + "\n")
                    self.log.see("end")
                    self.log.config(state="disabled")
                elif kind == "progress":
                    self.bar["value"] = val
                elif kind == "done":
                    self.running = False
                    self.btn.config(state="normal")
        except queue.Empty:
            pass
        self.root.after(150, self.poll)

    def pick_files(self):
        paths = filedialog.askopenfilenames(
            initialdir=self.cfg.get("last_dir") or None,
            filetypes=[("音訊/影片", "*.wav *.mp3 *.m4a *.aac *.flac *.mp4 *.mov *.mxf *.mkv *.avi *.webm"),
                       ("所有檔案", "*.*")])
        if paths:
            self.files = list(paths)
            self.cfg["last_dir"] = os.path.dirname(paths[0])
            self.lbl_files.config(text=f"已選 {len(paths)} 個檔案：{os.path.basename(paths[0])}…")

    def pick_model_dir(self):
        d = filedialog.askdirectory(title="選擇 CTranslate2 模型資料夾")
        if d:
            self.v_model.set(d)

    def save_settings(self):
        self.cfg.update({
            "model": self.v_model.get().strip(),
            "language": LANG_CHOICES[self.v_lang.get()],
            "device": self.v_dev.get(),
            "max_chars": int(self.v_max.get()),
            "beam_size": int(self.v_beam.get()),
            "prompt": self.v_prompt.get(),
            "strip_punct": self.v_strip.get(),
            "to_traditional": self.v_trad.get(),
            "vad": self.v_vad.get(),
            "save_txt": self.v_txt.get(),
            "theme": THEME_LABELS.get(self.v_theme.get(), "light"),
        })
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def start(self):
        if self.running:
            return
        if not self.files:
            messagebox.showinfo("提示", "請先選擇檔案")
            return
        self.save_settings()
        self.running = True
        self.btn.config(state="disabled")
        self.bar["value"] = 0
        threading.Thread(target=self.work, args=(dict(self.cfg), list(self.files)), daemon=True).start()

    # ---- 轉錄
    def load_model(self, cfg):
        from faster_whisper import WhisperModel
        device = cfg["device"]
        if device == "auto":
            try:
                import ctranslate2
                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:
                device = "cpu"
        compute = "float16" if device == "cuda" else "int8"
        key = (cfg["model"], device, compute)
        if self.model is None or self.model_key != key:
            self.say(f"載入模型 {cfg['model']} ({device}/{compute})…第一次會下載模型，請耐心等候")
            self.model = WhisperModel(cfg["model"], device=device, compute_type=compute)
            self.model_key = key
        return self.model

    def work(self, cfg, files):
        try:
            model = self.load_model(cfg)
            converter = None
            if cfg["to_traditional"]:
                try:
                    from opencc import OpenCC
                    converter = OpenCC("s2hk").convert
                except Exception:
                    self.say("(未安裝 opencc，略過繁體轉換)")

            for idx, path in enumerate(files, 1):
                self.say(f"[{idx}/{len(files)}] {os.path.basename(path)}")
                segments, info = model.transcribe(
                    path,
                    language=cfg["language"],
                    beam_size=cfg["beam_size"],
                    vad_filter=cfg["vad"],
                    word_timestamps=True,
                    initial_prompt=cfg["prompt"] or None,
                    condition_on_previous_text=False,
                )
                total = max(info.duration, 1)
                entries = []
                for seg in segments:
                    chars = segment_to_chars(seg, converter)
                    for line in split_lines(chars, cfg["max_chars"]):
                        text = tidy("".join(x["c"] for x in line), cfg["strip_punct"])
                        if not text:
                            continue
                        entries.append([line[0]["s"], line[-1]["e"], text])
                    self.q.put(("progress", min(100, seg.end / total * 100)))
                    self.say(f"  {fmt_time(seg.start)}  {seg.text.strip()[:40]}")

                # 確保時間冇重疊
                for i in range(len(entries) - 1):
                    if entries[i][1] > entries[i + 1][0]:
                        entries[i][1] = entries[i + 1][0]
                    if entries[i][1] <= entries[i][0]:
                        entries[i][1] = entries[i][0] + 0.2

                base = os.path.splitext(path)[0]
                with open(base + ".srt", "w", encoding="utf-8") as f:
                    for n, (s, e, t) in enumerate(entries, 1):
                        f.write(f"{n}\n{fmt_time(s)} --> {fmt_time(e)}\n{t}\n\n")
                if cfg["save_txt"]:
                    with open(base + ".txt", "w", encoding="utf-8") as f:
                        f.write("\n".join(t for _, _, t in entries))
                self.say(f"  完成 → {base}.srt ({len(entries)} 行)")
            self.q.put(("progress", 100))
            self.say("全部完成。")
        except Exception as ex:
            self.say(f"出錯：{ex}")
        finally:
            self.q.put(("done", None))


def enable_hidpi():
    """Windows 高解像度螢幕：宣告程式支援 DPI，避免畫面被系統放大而變模糊。"""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


if __name__ == "__main__":
    enable_hidpi()  # 必須喺建立視窗之前呼叫
    root = tk.Tk()
    App(root)
    root.mainloop()
