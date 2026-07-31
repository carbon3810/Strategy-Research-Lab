from __future__ import annotations

import json
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

from controller import ResearchController, ResearchRequest
from core import (
    app_dir,
    diagnose_mt5_csv,
    ensure_external_assets,
    format_csv_diagnosis,
    load_plugins,
)


class SettingsValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


def validate_settings_dict(cfg: Any) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(cfg, dict):
        raise SettingsValidationError(
            ["ルート要素: JSONオブジェクト（{ ... }）である必要があります。"]
        )

    def require_int(
        path: str,
        default: int | None = None,
        min_value: int | None = None,
        max_value: int | None = None,
    ) -> int | None:
        value: Any = cfg
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                if default is not None:
                    return default
                errors.append(f"{path}: 項目がありません。")
                return None
            value = value[part]
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{path}: 整数で指定してください。現在値={value!r}")
            return None
        if min_value is not None and value < min_value:
            errors.append(f"{path}: {min_value}以上にしてください。現在値={value}")
        if max_value is not None and value > max_value:
            errors.append(f"{path}: {max_value}以下にしてください。現在値={value}")
        return value

    def require_num_list(
        path: str,
        default: list[float] | None = None,
        positive: bool = False,
    ) -> list[float]:
        value = cfg.get(path, default)
        if value is None:
            errors.append(f"{path}: 項目がありません。")
            return []
        if not isinstance(value, list) or not value:
            errors.append(
                f"{path}: 1件以上の数値配列で指定してください。現在値={value!r}"
            )
            return []
        result: list[float] = []
        for index, item in enumerate(value):
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                errors.append(
                    f"{path}[{index}]: 数値で指定してください。現在値={item!r}"
                )
                continue
            if positive and item <= 0:
                errors.append(
                    f"{path}[{index}]: 0より大きい値にしてください。現在値={item}"
                )
            result.append(float(item))
        return result

    require_int("max_conditions", min_value=1, max_value=6)
    require_int("max_candidates", min_value=1, max_value=100000)
    require_int("min_trades", min_value=1, max_value=1000000)
    require_int("max_hold_bars", min_value=1, max_value=100000)
    require_int("atr_period", min_value=2, max_value=1000)
    require_int("slope_lookback", min_value=1, max_value=1000)
    require_int("breakout_lookback", min_value=2, max_value=10000)

    ema_periods = require_num_list("ema_periods", positive=True)
    for index, value in enumerate(ema_periods):
        if int(value) != value:
            errors.append(
                f"ema_periods[{index}]: EMA期間は整数にしてください。現在値={value}"
            )

    require_num_list("rr_values", positive=True)
    require_num_list("stop_mult_values", positive=True)

    sides = cfg.get("sides")
    if not isinstance(sides, list) or not sides:
        errors.append(
            f"sides: 'BUY' または 'SELL' の配列で指定してください。現在値={sides!r}"
        )
    else:
        for index, side in enumerate(sides):
            if side not in ("BUY", "SELL"):
                errors.append(
                    f"sides[{index}]: 'BUY' または 'SELL' のみ指定できます。"
                    f"現在値={side!r}"
                )

    split = cfg.get("split_years")
    if not isinstance(split, dict):
        errors.append(
            f"split_years: オブジェクトで指定してください。現在値={split!r}"
        )
    else:
        train_end = split.get("train_end")
        valid_end = split.get("valid_end")
        if isinstance(train_end, bool) or not isinstance(train_end, int):
            errors.append(
                "split_years.train_end: 年を整数で指定してください。"
                f"現在値={train_end!r}"
            )
        if isinstance(valid_end, bool) or not isinstance(valid_end, int):
            errors.append(
                "split_years.valid_end: 年を整数で指定してください。"
                f"現在値={valid_end!r}"
            )
        if (
            isinstance(train_end, int)
            and isinstance(valid_end, int)
            and train_end >= valid_end
        ):
            errors.append(
                f"split_years: train_end({train_end}) は "
                f"valid_end({valid_end}) より前にしてください。"
            )

    if not isinstance(cfg.get("plugin_params", {}), dict):
        errors.append("plugin_params: オブジェクトで指定してください。")

    if errors:
        raise SettingsValidationError(errors)
    return cfg


def load_settings_file(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        raise SettingsValidationError(
            [f"ファイル: 指定された設定JSONが見つかりません。\n{file_path}"]
        )
    if file_path.suffix.lower() != ".json":
        raise SettingsValidationError(
            [f"ファイル形式: .json ファイルを選択してください。\n選択={file_path.name}"]
        )
    try:
        text = file_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as error:
        raise SettingsValidationError([
            "文字コード: UTF-8またはUTF-8 BOM付きで保存してください。",
            f"デコード位置: byte {error.start}",
            f"ファイル: {file_path}",
        ]) from error

    try:
        cfg = json.loads(text)
    except json.JSONDecodeError as error:
        lines = text.splitlines()
        bad_line = lines[error.lineno - 1] if 1 <= error.lineno <= len(lines) else ""
        pointer = " " * max(error.colno - 1, 0) + "^"
        raise SettingsValidationError([
            f"JSON構文エラー: {error.msg}",
            f"場所: {error.lineno}行 {error.colno}列",
            f"該当行: {bad_line}",
            f"        {pointer}",
            f"ファイル: {file_path}",
        ]) from error

    return validate_settings_dict(cfg)


class ToolTip:
    """Tkinterウィジェットにマウスオーバー説明を表示します。"""

    def __init__(
        self,
        widget: tk.Widget,
        text: str,
        *,
        delay_ms: int = 350,
        wraplength: int = 430,
    ) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self.wraplength = wraplength
        self._after_id: str | None = None
        self._window: tk.Toplevel | None = None

        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: tk.Event | None = None) -> None:
        self._cancel_schedule()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel_schedule(self) -> None:
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self) -> None:
        self._after_id = None
        if self._window is not None or not self.text:
            return

        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6

        window = tk.Toplevel(self.widget)
        self._window = window
        window.wm_overrideredirect(True)
        try:
            window.attributes("-topmost", True)
        except tk.TclError:
            pass
        window.wm_geometry(f"+{x}+{y}")

        frame = tk.Frame(
            window,
            background="#fffbe6",
            relief="solid",
            borderwidth=1,
        )
        frame.pack()
        label = tk.Label(
            frame,
            text=self.text,
            justify="left",
            background="#fffbe6",
            foreground="#222222",
            padx=10,
            pady=8,
            wraplength=self.wraplength,
        )
        label.pack()

    def _hide(self, _event: tk.Event | None = None) -> None:
        self._cancel_schedule()
        if self._window is not None:
            self._window.destroy()
            self._window = None


class StrategyResearchLabApp(tk.Tk):
    PHASE_RANGES = {
        "lower_csv": (0.00, 0.08),
        "lower_features": (0.08, 0.25),
        "higher_csv": (0.25, 0.30),
        "higher_features": (0.30, 0.42),
        "align": (0.42, 0.50),
        "conditions": (0.50, 0.55),
        "outcomes": (0.55, 0.70),
        "search": (0.70, 0.95),
        "save": (0.95, 1.00),
    }

    def __init__(self) -> None:
        super().__init__()
        self.title("Strategy Research Lab")
        self.geometry("1180x820")
        self.minsize(980, 720)

        ensure_external_assets()
        self.controller = ResearchController()
        self.cfg: dict[str, Any] = {}
        self.vars = {
            key: tk.StringVar()
            for key in ("htf", "ltf", "out", "preset")
        }
        self.inputs: dict[str, tk.StringVar] = {}
        self.run_started_at: float | None = None
        self.phase_started_at: float | None = None
        self.current_phase = ""
        self.running = False

        self._build()
        self.reload_plugins()
        self.load_preset(app_dir() / "presets" / "default.json")

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 6}

        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=10)
        ttk.Label(
            top, text="Strategy Research Lab", font=("", 18, "bold")
        ).pack(side="left")
        ttk.Label(
            top, text="拡張型・研究用", foreground="#666"
        ).pack(side="left", padx=12)

        files = ttk.LabelFrame(self, text="データ")
        files.pack(fill="x", padx=12, pady=6)
        self._file_row(
            files, 0, "上位足CSV", self.vars["htf"], "上位足を診断"
        )
        self._file_row(
            files, 1, "下位足CSV", self.vars["ltf"], "下位足を診断"
        )
        self._dir_row(files, 2, "出力フォルダ", self.vars["out"])

        preset = ttk.LabelFrame(
            self,
            text="設定JSON・プリセット（settings_used.jsonも読込可能）",
        )
        preset.pack(fill="x", padx=12, pady=6)
        ttk.Entry(
            preset, textvariable=self.vars["preset"]
        ).grid(row=0, column=0, sticky="ew", **pad)
        ttk.Button(
            preset, text="設定JSONを選択・読込", command=self.choose_preset
        ).grid(row=0, column=1, **pad)
        ttk.Button(
            preset, text="現在設定を保存", command=self.save_preset
        ).grid(row=0, column=2, **pad)
        ttk.Button(
            preset, text="プラグイン再読込", command=self.reload_plugins
        ).grid(row=0, column=3, **pad)
        preset.columnconfigure(0, weight=1)

        settings = ttk.LabelFrame(self, text="主要設定")
        settings.pack(fill="x", padx=12, pady=6)
        setting_items = [
            (
                "最大売買条件数",
                "max_conditions",
                "1つの売買ルールに組み合わせる条件の最大数です。\n\n"
                "例：\n"
                "・価格がEMA200より上\n"
                "・RSIが30以下\n"
                "・NY時間\n"
                "この場合は3条件です。\n\n"
                "条件を増やすほど複雑なルールを探索できますが、"
                "処理時間と過剰最適化の危険も増えます。\n\n"
                "初期値・推奨値：3",
            ),
            (
                "探索候補数",
                "max_candidates",
                "評価する条件の組み合わせ数です。\n\n"
                "数を増やすほど広く探索できますが、処理時間も長くなります。\n\n"
                "目安：\n"
                "・100：動作確認向け\n"
                "・300：標準\n"
                "・1000以上：詳細探索向け\n\n"
                "初期値・推奨値：300",
            ),
            (
                "採用する最低年間取引数",
                "min_trades",
                "学習期間の各年について、最低限必要とする取引回数です。\n\n"
                "取引回数が少なすぎる候補は、偶然よく見えている可能性があるため除外します。\n\n"
                "例：100なら、学習期間が5年間の場合、"
                "学習期間全体では原則500回以上が必要です。\n\n"
                "初期値・推奨値：100回／年",
            ),
            (
                "時間切れ決済（下位足本数）",
                "max_hold_bars",
                "エントリー後、最大で何本の下位足ローソク足を保有するかを設定します。\n\n"
                "SL・TPに到達しないまま指定本数を超えた場合は、時間切れで決済します。\n\n"
                "例：下位足がM5で48本なら、48×5分＝240分、約4時間です。\n"
                "上位足H1の48本ではありません。\n\n"
                "初期値・推奨値：48",
            ),
            (
                "学習データ終了年",
                "train_end",
                "売買ルールを作るために使用する学習期間の終了年です。\n\n"
                "例：CSVが2019年開始で設定が2023なら、"
                "2019～2023年を学習データとして使用します。\n\n"
                "初期値：2023",
            ),
            (
                "検証データ終了年",
                "valid_end",
                "学習データで見つけたルールを確認する検証期間の終了年です。\n\n"
                "例：学習終了年が2023、検証終了年が2024なら、\n"
                "・～2023年：学習\n"
                "・2024年：検証\n"
                "・2025年以降：OOS（未知データ）\n"
                "として評価します。\n\n"
                "初期値：2024",
            ),
        ]
        self._tooltips: list[ToolTip] = []
        for index, (label, key, help_text) in enumerate(setting_items):
            label_row = index // 3 * 2
            column = index % 3

            label_frame = ttk.Frame(settings)
            label_frame.grid(
                row=label_row,
                column=column,
                sticky="w",
                **pad,
            )
            ttk.Label(label_frame, text=label).pack(side="left")
            help_label = ttk.Label(
                label_frame,
                text=" ？ ",
                cursor="question_arrow",
                foreground="#1f5fa8",
                font=("", 9, "bold"),
            )
            help_label.pack(side="left", padx=(4, 0))
            self._tooltips.append(ToolTip(help_label, help_text))

            variable = tk.StringVar()
            self.inputs[key] = variable
            entry = ttk.Entry(
                settings,
                textvariable=variable,
                width=16,
            )
            entry.grid(
                row=label_row + 1,
                column=column,
                sticky="ew",
                **pad,
            )
            self._tooltips.append(ToolTip(entry, help_text))
            settings.columnconfigure(column, weight=1)

        progress_box = ttk.LabelFrame(self, text="処理状況")
        progress_box.pack(fill="x", padx=12, pady=8)

        action = ttk.Frame(progress_box)
        action.pack(fill="x", padx=8, pady=(8, 4))
        self.run_btn = ttk.Button(
            action, text="自動探索を開始", command=self.start
        )
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(
            action,
            text="途中中断",
            command=self.request_cancel,
            state="disabled",
        )
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.status = tk.StringVar(value="準備完了")
        ttk.Label(
            action, textvariable=self.status, font=("", 10, "bold")
        ).pack(side="right")

        ttk.Label(progress_box, text="全体進捗").pack(anchor="w", padx=8)
        self.overall_progress = ttk.Progressbar(
            progress_box, mode="determinate", maximum=100
        )
        self.overall_progress.pack(fill="x", padx=8, pady=(2, 6))

        stage_line = ttk.Frame(progress_box)
        stage_line.pack(fill="x", padx=8)
        self.stage_var = tk.StringVar(value="STEP --/--：待機中")
        ttk.Label(
            stage_line, textvariable=self.stage_var, font=("", 10, "bold")
        ).pack(side="left")
        self.stage_percent_var = tk.StringVar(value="")
        ttk.Label(
            stage_line, textvariable=self.stage_percent_var
        ).pack(side="right")

        self.stage_progress = ttk.Progressbar(
            progress_box, mode="determinate", maximum=100
        )
        self.stage_progress.pack(fill="x", padx=8, pady=(2, 6))

        time_line = ttk.Frame(progress_box)
        time_line.pack(fill="x", padx=8, pady=(0, 4))
        self.elapsed_var = tk.StringVar(value="経過時間: --")
        self.remaining_var = tk.StringVar(value="残り時間: --")
        self.finish_var = tk.StringVar(value="予想終了: --")
        ttk.Label(time_line, textvariable=self.elapsed_var).pack(side="left")
        ttk.Label(
            time_line, textvariable=self.remaining_var
        ).pack(side="left", padx=28)
        ttk.Label(time_line, textvariable=self.finish_var).pack(side="left")

        self.current_work_var = tk.StringVar(value="現在: --")
        ttk.Label(
            progress_box,
            textvariable=self.current_work_var,
            wraplength=1120,
        ).pack(anchor="w", padx=8, pady=(0, 3))

        self.next_work_var = tk.StringVar(value="次の処理: --")
        ttk.Label(
            progress_box,
            textvariable=self.next_work_var,
            wraplength=1120,
        ).pack(anchor="w", padx=8, pady=(0, 8))

        log_frame = ttk.LabelFrame(self, text="ログ")
        log_frame.pack(fill="both", expand=True, padx=12, pady=6)
        self.log = tk.Text(log_frame, wrap="word", height=14)
        self.log.pack(fill="both", expand=True, padx=6, pady=6)

        ttk.Label(
            self,
            text=(
                "新しい指標・シグナルはEXEと同じ場所のpluginsフォルダへ追加します。"
                "設定値はpresetで変更できます。"
            ),
            foreground="#555",
        ).pack(fill="x", padx=14, pady=(0, 8))

    def _file_row(
        self,
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.StringVar,
        diagnosis_label: str,
    ) -> None:
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", padx=8, pady=6
        )
        ttk.Entry(parent, textvariable=variable).grid(
            row=row, column=1, sticky="ew", padx=8, pady=6
        )
        ttk.Button(
            parent, text="選択", command=lambda: self.pick_file(variable)
        ).grid(row=row, column=2, padx=8, pady=6)
        ttk.Button(
            parent,
            text=diagnosis_label,
            command=lambda: self.start_csv_diagnosis(variable.get(), label),
        ).grid(row=row, column=3, padx=8, pady=6)
        parent.columnconfigure(1, weight=1)

    def _dir_row(
        self,
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.StringVar,
    ) -> None:
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", padx=8, pady=6
        )
        ttk.Entry(parent, textvariable=variable).grid(
            row=row, column=1, sticky="ew", padx=8, pady=6
        )
        ttk.Button(
            parent, text="選択", command=lambda: self.pick_dir(variable)
        ).grid(row=row, column=2, padx=8, pady=6)

    def pick_file(self, variable: tk.StringVar) -> None:
        path = filedialog.askopenfilename(
            filetypes=[("CSV", "*.csv"), ("All", "*.*")]
        )
        if path:
            variable.set(path)

    def pick_dir(self, variable: tk.StringVar) -> None:
        path = filedialog.askdirectory()
        if path:
            variable.set(path)

    def write(self, text: Any) -> None:
        self.log.insert("end", str(text) + "\n")
        self.log.see("end")

    def ui_write(self, text: Any) -> None:
        self.after(0, lambda: self.write(text))

    def start_csv_diagnosis(self, path: str, label: str) -> None:
        if not path:
            messagebox.showwarning("CSV診断", f"{label}を選択してください。")
            return
        self.status.set(f"{label}を診断中")
        self.write(f"{label}のCSV診断を開始: {path}")
        threading.Thread(
            target=self._csv_diagnosis_worker,
            args=(path, label),
            daemon=True,
        ).start()

    def _csv_diagnosis_worker(self, path: str, label: str) -> None:
        try:
            report = diagnose_mt5_csv(path)
            text = format_csv_diagnosis(report)
        except Exception:
            error_text = traceback.format_exc()
            self.after(0, lambda: self.write(error_text))
            self.after(
                0,
                lambda: messagebox.showerror(
                    "CSV診断エラー", error_text[-1800:]
                ),
            )
            return

        def show_result() -> None:
            self.write(text)
            self.status.set("診断完了")
            if report.get("ok"):
                messagebox.showinfo(
                    f"{label} CSV診断：読込可能",
                    "CSVを正常に読み込めます。\n\n"
                    f"有効ローソク足: {report['valid_bars']:,}本\n"
                    f"推定時間足: {report['timeframe']}\n"
                    f"期間: {report['first']} ～ {report['last']}\n"
                    f"形式: {report['layout']}\n"
                    f"文字コード: {report['encoding']}",
                )
            else:
                problems = report.get("problems") or [
                    "原因を特定できませんでした。"
                ]
                details = "\n".join(f"・{item}" for item in problems)
                messagebox.showerror(
                    f"{label} CSV診断：読込不可",
                    "CSVを読み込めません。ログ欄に詳細を表示しました。\n\n"
                    + details[:1800],
                )

        self.after(0, show_result)

    def reload_plugins(self) -> None:
        try:
            loaded = load_plugins()
            self.write(
                "プラグイン: " + (", ".join(loaded) if loaded else "なし")
            )
        except Exception as error:
            messagebox.showerror("プラグインエラー", str(error))

    def choose_preset(self) -> None:
        path = filedialog.askopenfilename(
            initialdir=app_dir() / "presets",
            filetypes=[("JSON", "*.json")],
        )
        if path:
            self.load_preset(Path(path))

    def load_preset(self, path: str | Path) -> None:
        file_path = Path(path)
        try:
            cfg = load_settings_file(file_path)
            self.cfg = cfg
            self.vars["preset"].set(str(file_path))
            values = {
                "max_conditions": cfg.get("max_conditions", 3),
                "max_candidates": cfg.get("max_candidates", 300),
                "min_trades": cfg.get("min_trades", 100),
                "max_hold_bars": cfg.get("max_hold_bars", 48),
                "train_end": cfg.get("split_years", {}).get("train_end", 2023),
                "valid_end": cfg.get("split_years", {}).get("valid_end", 2024),
            }
            for key, value in values.items():
                self.inputs[key].set(str(value))
            self.write(f"設定JSON読込成功: {file_path}")
        except SettingsValidationError as error:
            details = "\n".join(f"・{item}" for item in error.errors)
            self.write("設定JSON読込失敗:\n" + details)
            messagebox.showerror(
                "設定JSONを読み込めません",
                "次の問題を修正してください。\n\n" + details,
            )
        except Exception:
            details = traceback.format_exc()
            self.write(details)
            messagebox.showerror("設定JSON読込エラー", details[-1800:])

    def sync_cfg(self) -> dict[str, Any]:
        cfg = dict(self.cfg)
        labels = {
            "max_conditions": "最大売買条件数",
            "max_candidates": "探索候補数",
            "min_trades": "採用する最低年間取引数",
            "max_hold_bars": "時間切れ決済（下位足本数）",
            "train_end": "学習データ終了年",
            "valid_end": "検証データ終了年",
        }
        parsed: dict[str, int] = {}
        errors: list[str] = []
        for key in labels:
            raw = self.inputs[key].get().strip()
            try:
                parsed[key] = int(raw)
            except ValueError:
                errors.append(
                    f"{labels[key]}: 整数で入力してください。現在値={raw!r}"
                )
        if errors:
            raise SettingsValidationError(errors)

        for key in (
            "max_conditions",
            "max_candidates",
            "min_trades",
            "max_hold_bars",
        ):
            cfg[key] = parsed[key]
        cfg.setdefault("split_years", {})
        cfg["split_years"]["train_end"] = parsed["train_end"]
        cfg["split_years"]["valid_end"] = parsed["valid_end"]
        return validate_settings_dict(cfg)

    def save_preset(self) -> None:
        try:
            path = filedialog.asksaveasfilename(
                initialdir=app_dir() / "presets",
                defaultextension=".json",
                filetypes=[("JSON", "*.json")],
            )
            if path:
                Path(path).write_text(
                    json.dumps(
                        self.sync_cfg(), ensure_ascii=False, indent=2
                    ),
                    encoding="utf-8",
                )
                self.vars["preset"].set(path)
                self.write(f"プリセット保存: {path}")
        except SettingsValidationError as error:
            details = "\n".join(f"・{item}" for item in error.errors)
            self.write("設定保存失敗:\n" + details)
            messagebox.showerror(
                "設定を保存できません",
                "次の問題を修正してください。\n\n" + details,
            )
        except Exception as error:
            messagebox.showerror("保存エラー", str(error))

    @staticmethod
    def _format_seconds(seconds: float | None) -> str:
        if seconds is None or seconds < 0:
            return "--"
        value = int(seconds)
        hours, remainder = divmod(value, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}時間{minutes}分{secs}秒"
        if minutes:
            return f"{minutes}分{secs}秒"
        return f"{secs}秒"

    def start(self) -> None:
        if self.running:
            return
        if not self.vars["ltf"].get() or not self.vars["out"].get():
            messagebox.showwarning(
                "不足", "下位足CSVと出力フォルダを選択してください。"
            )
            return
        try:
            cfg = self.sync_cfg()
        except SettingsValidationError as error:
            details = "\n".join(f"・{item}" for item in error.errors)
            messagebox.showerror("設定エラー", details)
            return

        self.running = True
        self.run_started_at = time.time()
        self.phase_started_at = self.run_started_at
        self.current_phase = ""
        self.controller.reset_cancel()

        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.overall_progress.configure(value=0)
        self.stage_progress.configure(value=0)
        self.status.set("処理中")
        self.elapsed_var.set("経過時間: 0秒")
        self.remaining_var.set("残り時間: 計算中...")
        self.finish_var.set("予想終了: 計算中...")
        self.current_work_var.set("現在: 開始準備中")
        self.next_work_var.set("次の処理: 下位足CSV読込")

        request = ResearchRequest(
            lower_csv=self.vars["ltf"].get(),
            higher_csv=self.vars["htf"].get(),
            output_dir=self.vars["out"].get(),
            settings=cfg,
        )
        threading.Thread(
            target=self._research_worker,
            args=(request,),
            daemon=True,
        ).start()

    def request_cancel(self) -> None:
        if not self.running:
            return
        self.controller.request_cancel()
        self.stop_btn.configure(state="disabled")
        self.status.set("中断要求を受付")
        self.current_work_var.set(
            "現在: 安全に停止するため、現在の計算単位を終了中"
        )
        self.next_work_var.set("次の処理: 中断")
        self.write("途中中断を要求しました。")

    def _research_worker(self, request: ResearchRequest) -> None:
        try:
            result = self.controller.run(
                request,
                on_progress=self._thread_progress,
                on_log=self.ui_write,
            )
            self.after(
                0,
                lambda: self._finish_success(
                    result.candidate_count, result.output_dir
                ),
            )
        except InterruptedError as error:
            self.after(0, lambda: self._finish_cancelled(str(error)))
        except SettingsValidationError as error:
            details = "\n".join(f"・{item}" for item in error.errors)
            self.after(0, lambda: self._finish_error("設定エラー", details))
        except ValueError as error:
            self.after(
                0,
                lambda: self._finish_error(
                    "CSVまたは入力データのエラー", str(error)
                ),
            )
        except Exception:
            details = traceback.format_exc()
            self.after(0, lambda: self._finish_error("エラー", details))

    def _thread_progress(
        self,
        phase: str,
        done: int,
        total: int,
        detail: dict[str, Any],
    ) -> None:
        self.after(
            0,
            lambda: self._apply_progress(phase, done, total, detail),
        )

    def _apply_progress(
        self,
        phase: str,
        done: int,
        total: int,
        detail: dict[str, Any],
    ) -> None:
        now = time.time()
        if phase != self.current_phase:
            self.current_phase = phase
            self.phase_started_at = now

        total = max(int(total), 1)
        done = max(0, min(int(done), total))
        fraction = done / total

        step = int(detail.get("step", 0))
        total_steps = int(detail.get("total_steps", 9))
        message = str(detail.get("message", "処理中"))
        next_message = str(detail.get("next", "--"))

        self.stage_var.set(f"STEP {step}/{total_steps}：{message}")
        self.stage_percent_var.set(f"{fraction * 100:.1f}%")
        self.stage_progress.configure(maximum=total, value=done)
        self.current_work_var.set(f"現在: {message}")
        self.next_work_var.set(f"次の処理: {next_message}")

        start, end = self.PHASE_RANGES.get(
            phase, ((step - 1) / total_steps, step / total_steps)
        )
        overall_fraction = start + (end - start) * fraction
        self.overall_progress.configure(value=overall_fraction * 100)

        elapsed = now - (self.run_started_at or now)
        phase_elapsed = max(now - (self.phase_started_at or now), 0.001)
        rate = done / phase_elapsed if done > 0 else 0.0
        remaining = (total - done) / rate if rate > 0 else None

        self.elapsed_var.set(
            f"経過時間: {self._format_seconds(elapsed)}"
        )
        remaining_suffix = ""
        if phase == "search":
            remaining_suffix = f"（残り{total - done:,}件）"
        self.remaining_var.set(
            f"残り時間: 約{self._format_seconds(remaining)}{remaining_suffix}"
        )
        if remaining is not None:
            finish = datetime.fromtimestamp(now + remaining)
            self.finish_var.set(
                f"予想終了: {finish.strftime('%H:%M:%S')}"
            )
        else:
            self.finish_var.set("予想終了: 計算中...")
        self.status.set(
            f"{done:,}/{total:,}" if total > 1 else f"STEP {step}/{total_steps}"
        )

    def _finish_success(self, candidate_count: int, output_dir: str) -> None:
        self.running = False
        self.run_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.overall_progress.configure(value=100)
        self.stage_progress.configure(value=100, maximum=100)
        self.stage_percent_var.set("100%")
        self.status.set("完了")
        self.remaining_var.set("残り時間: 0秒")
        self.finish_var.set(
            f"終了時刻: {datetime.now().strftime('%H:%M:%S')}"
        )
        self.current_work_var.set(
            f"現在: 完了（採用候補 {candidate_count:,}件）"
        )
        self.next_work_var.set("次の処理: strategy_ranking.csvを確認")
        self.write(
            f"完了: {output_dir} / 候補 {candidate_count:,}件"
        )
        messagebox.showinfo(
            "完了",
            "自動探索が完了しました。\n"
            f"候補数: {candidate_count:,}\n\n"
            "strategy_ranking.csvをGPTへ渡して研究を続けてください。",
        )

    def _finish_cancelled(self, details: str) -> None:
        self.running = False
        self.run_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status.set("中断済み")
        self.remaining_var.set("残り時間: 中断")
        self.finish_var.set(
            f"中断時刻: {datetime.now().strftime('%H:%M:%S')}"
        )
        self.current_work_var.set("現在: 中断されました")
        self.next_work_var.set("次の処理: 設定を確認して再実行")
        self.write(details)
        messagebox.showinfo("中断", details)

    def _finish_error(self, title: str, details: str) -> None:
        self.running = False
        self.run_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status.set("エラー")
        self.current_work_var.set("現在: エラーで停止")
        self.next_work_var.set("次の処理: ログを確認")
        self.write(details)
        messagebox.showerror(title, details[-1800:])
