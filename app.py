from __future__ import annotations
import json, threading, traceback, time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from core import app_dir, ensure_external_assets, load_plugins, diagnose_mt5_csv, format_csv_diagnosis, read_mt5_csv, add_builtin_features, align_higher, auto_discover, save_outputs

class SettingsValidationError(Exception):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("\n".join(errors))

def validate_settings_dict(cfg):
    """Return a normalized settings dict or raise SettingsValidationError with field-level messages."""
    errors = []
    if not isinstance(cfg, dict):
        raise SettingsValidationError(["ルート要素: JSONオブジェクト（{ ... }）である必要があります。"])

    def require_int(path, default=None, min_value=None, max_value=None):
        value = cfg
        parts = path.split(".")
        for part in parts:
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

    def require_num_list(path, default=None, positive=False):
        value = cfg.get(path, default)
        if value is None:
            errors.append(f"{path}: 項目がありません。")
            return []
        if not isinstance(value, list) or not value:
            errors.append(f"{path}: 1件以上の数値配列で指定してください。現在値={value!r}")
            return []
        out = []
        for i, item in enumerate(value):
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                errors.append(f"{path}[{i}]: 数値で指定してください。現在値={item!r}")
                continue
            if positive and item <= 0:
                errors.append(f"{path}[{i}]: 0より大きい値にしてください。現在値={item}")
            out.append(item)
        return out

    require_int("max_conditions", min_value=1, max_value=6)
    require_int("max_candidates", min_value=1, max_value=100000)
    require_int("min_trades", min_value=1, max_value=1000000)
    require_int("max_hold_bars", min_value=1, max_value=100000)
    require_int("atr_period", min_value=2, max_value=1000)
    require_int("slope_lookback", min_value=1, max_value=1000)
    require_int("breakout_lookback", min_value=2, max_value=10000)

    ema = require_num_list("ema_periods", positive=True)
    for i, v in enumerate(ema):
        if int(v) != v:
            errors.append(f"ema_periods[{i}]: EMA期間は整数にしてください。現在値={v}")

    require_num_list("rr_values", positive=True)
    require_num_list("stop_mult_values", positive=True)

    sides = cfg.get("sides")
    if not isinstance(sides, list) or not sides:
        errors.append(f"sides: 'BUY' または 'SELL' の配列で指定してください。現在値={sides!r}")
    else:
        for i, side in enumerate(sides):
            if side not in ("BUY", "SELL"):
                errors.append(f"sides[{i}]: 'BUY' または 'SELL' のみ指定できます。現在値={side!r}")

    split = cfg.get("split_years")
    if not isinstance(split, dict):
        errors.append(f"split_years: オブジェクトで指定してください。現在値={split!r}")
    else:
        te = split.get("train_end")
        ve = split.get("valid_end")
        if isinstance(te, bool) or not isinstance(te, int):
            errors.append(f"split_years.train_end: 年を整数で指定してください。現在値={te!r}")
        if isinstance(ve, bool) or not isinstance(ve, int):
            errors.append(f"split_years.valid_end: 年を整数で指定してください。現在値={ve!r}")
        if isinstance(te, int) and isinstance(ve, int) and te >= ve:
            errors.append(f"split_years: train_end({te}) は valid_end({ve}) より前にしてください。")

    plugin_params = cfg.get("plugin_params", {})
    if not isinstance(plugin_params, dict):
        errors.append(f"plugin_params: オブジェクトで指定してください。現在値={plugin_params!r}")

    if errors:
        raise SettingsValidationError(errors)
    return cfg

def load_settings_file(path):
    p = Path(path)
    if not p.exists():
        raise SettingsValidationError([f"ファイル: 指定された設定JSONが見つかりません。\n{p}"])
    if p.suffix.lower() != ".json":
        raise SettingsValidationError([f"ファイル形式: .json ファイルを選択してください。\n選択={p.name}"])
    try:
        text = p.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        raise SettingsValidationError([
            "文字コード: UTF-8またはUTF-8 BOM付きで保存してください。",
            f"デコード位置: byte {e.start}",
            f"ファイル: {p}"
        ])
    try:
        cfg = json.loads(text)
    except json.JSONDecodeError as e:
        bad_line = ""
        lines = text.splitlines()
        if 1 <= e.lineno <= len(lines):
            bad_line = lines[e.lineno - 1]
        pointer = " " * max(e.colno - 1, 0) + "^"
        raise SettingsValidationError([
            f"JSON構文エラー: {e.msg}",
            f"場所: {e.lineno}行 {e.colno}列",
            f"該当行: {bad_line}",
            f"        {pointer}",
            f"ファイル: {p}"
        ])
    return validate_settings_dict(cfg)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Strategy Research Lab")
        self.geometry("1180x820")
        self.minsize(980,720)
        ensure_external_assets()
        self.cfg={}
        self.cancel_requested = False
        self.run_started_at = None
        self.last_progress_time = None
        self.stage_started_at = None
        self.stage_name = ""
        self.vars={k:tk.StringVar() for k in ("htf","ltf","out","preset")}
        self._build()
        self.reload_plugins()
        self.load_preset(app_dir()/"presets"/"default.json")

    def _build(self):
        pad={"padx":8,"pady":6}
        top=ttk.Frame(self); top.pack(fill="x",padx=12,pady=10)
        ttk.Label(top,text="Strategy Research Lab",font=("",18,"bold")).pack(side="left")
        ttk.Label(top,text="拡張型・研究用",foreground="#666").pack(side="left",padx=12)

        files=ttk.LabelFrame(self,text="データ"); files.pack(fill="x",padx=12,pady=6)
        self._file_row(files,0,"上位足CSV",self.vars["htf"],diagnosis_label="上位足を診断")
        self._file_row(files,1,"下位足CSV",self.vars["ltf"],diagnosis_label="下位足を診断")
        self._dir_row(files,2,"出力フォルダ",self.vars["out"])

        preset=ttk.LabelFrame(self,text="設定JSON・プリセット（settings_used.jsonも読込可能）"); preset.pack(fill="x",padx=12,pady=6)
        ttk.Entry(preset,textvariable=self.vars["preset"]).grid(row=0,column=0,sticky="ew",**pad)
        ttk.Button(preset,text="設定JSONを選択・読込",command=self.choose_preset).grid(row=0,column=1,**pad)
        ttk.Button(preset,text="現在設定を保存",command=self.save_preset).grid(row=0,column=2,**pad)
        ttk.Button(preset,text="プラグイン再読込",command=self.reload_plugins).grid(row=0,column=3,**pad)
        preset.columnconfigure(0,weight=1)

        settings=ttk.LabelFrame(self,text="主要設定"); settings.pack(fill="x",padx=12,pady=6)
        labels=[("最大条件数","max_conditions"),("最大候補数","max_candidates"),("最低取引数","min_trades"),
                ("最大保有本数","max_hold_bars"),("学習終了年","train_end"),("確認終了年","valid_end")]
        self.inputs={}
        for i,(lab,key) in enumerate(labels):
            ttk.Label(settings,text=lab).grid(row=i//3*2,column=i%3,sticky="w",**pad)
            v=tk.StringVar(); self.inputs[key]=v
            ttk.Entry(settings,textvariable=v,width=16).grid(row=i//3*2+1,column=i%3,sticky="ew",**pad)
            settings.columnconfigure(i%3,weight=1)

        progress_box=ttk.LabelFrame(self,text="処理状況")
        progress_box.pack(fill="x",padx=12,pady=8)

        action=ttk.Frame(progress_box)
        action.pack(fill="x",padx=8,pady=(8,4))
        self.run_btn=ttk.Button(action,text="自動探索を開始",command=self.start)
        self.run_btn.pack(side="left")
        self.stop_btn=ttk.Button(
            action,text="途中中断",command=self.request_cancel,state="disabled"
        )
        self.stop_btn.pack(side="left",padx=(8,0))
        self.status=tk.StringVar(value="準備完了")
        ttk.Label(action,textvariable=self.status,font=("",10,"bold")).pack(side="right")

        ttk.Label(progress_box,text="全体進捗").pack(anchor="w",padx=8)
        self.overall_progress=ttk.Progressbar(
            progress_box,mode="determinate",maximum=100
        )
        self.overall_progress.pack(fill="x",padx=8,pady=(2,6))

        stage_line=ttk.Frame(progress_box)
        stage_line.pack(fill="x",padx=8)
        self.stage_var=tk.StringVar(value="STEP --/--：待機中")
        ttk.Label(
            stage_line,textvariable=self.stage_var,font=("",10,"bold")
        ).pack(side="left")
        self.stage_percent_var=tk.StringVar(value="")
        ttk.Label(stage_line,textvariable=self.stage_percent_var).pack(side="right")

        self.progress=ttk.Progressbar(progress_box,mode="determinate")
        self.progress.pack(fill="x",padx=8,pady=(2,6))

        info=ttk.Frame(progress_box)
        info.pack(fill="x",padx=8,pady=(0,4))
        self.elapsed_var=tk.StringVar(value="経過時間: --")
        self.remaining_var=tk.StringVar(value="残り時間: --")
        self.finish_var=tk.StringVar(value="予想終了: --")
        ttk.Label(info,textvariable=self.elapsed_var).pack(side="left")
        ttk.Label(info,textvariable=self.remaining_var).pack(side="left",padx=28)
        ttk.Label(info,textvariable=self.finish_var).pack(side="left")

        self.current_work_var=tk.StringVar(value="現在: --")
        ttk.Label(
            progress_box,textvariable=self.current_work_var,wraplength=1120
        ).pack(anchor="w",padx=8,pady=(0,3))

        self.next_work_var=tk.StringVar(value="次の処理: --")
        ttk.Label(
            progress_box,textvariable=self.next_work_var,wraplength=1120
        ).pack(anchor="w",padx=8,pady=(0,8))

        logf=ttk.LabelFrame(self,text="ログ"); logf.pack(fill="both",expand=True,padx=12,pady=6)
        self.log=tk.Text(logf,wrap="word",height=18); self.log.pack(fill="both",expand=True,padx=6,pady=6)

        note=ttk.Label(self,text="新しい指標・シグナルはEXEと同じ場所の plugins フォルダへ追加します。設定値はpresetで変更できます。",
                       foreground="#555")
        note.pack(fill="x",padx=14,pady=(0,8))

    def _file_row(self,parent,row,label,var,diagnosis_label=None):
        ttk.Label(parent,text=label).grid(row=row,column=0,sticky="w",padx=8,pady=6)
        ttk.Entry(parent,textvariable=var).grid(row=row,column=1,sticky="ew",padx=8,pady=6)
        ttk.Button(parent,text="選択",command=lambda:self.pick_file(var)).grid(row=row,column=2,padx=8,pady=6)
        if diagnosis_label:
            ttk.Button(
                parent,
                text=diagnosis_label,
                command=lambda:self.start_csv_diagnosis(var.get(), label)
            ).grid(row=row,column=3,padx=8,pady=6)
        parent.columnconfigure(1,weight=1)

    def _dir_row(self,parent,row,label,var):
        ttk.Label(parent,text=label).grid(row=row,column=0,sticky="w",padx=8,pady=6)
        ttk.Entry(parent,textvariable=var).grid(row=row,column=1,sticky="ew",padx=8,pady=6)
        ttk.Button(parent,text="選択",command=lambda:self.pick_dir(var)).grid(row=row,column=2,padx=8,pady=6)

    def pick_file(self,var):
        p=filedialog.askopenfilename(filetypes=[("CSV","*.csv"),("All","*.*")])
        if p: var.set(p)
    def pick_dir(self,var):
        p=filedialog.askdirectory()
        if p: var.set(p)
    def write(self,s):
        self.log.insert("end",str(s)+"\n"); self.log.see("end"); self.update_idletasks()

    def start_csv_diagnosis(self, path, label):
        if not path:
            messagebox.showwarning("CSV診断", f"{label}を選択してください。")
            return
        self.status.set(f"{label}を診断中")
        self.write(f"{label}のCSV診断を開始: {path}")
        threading.Thread(
            target=self.csv_diagnosis_worker,
            args=(path, label),
            daemon=True
        ).start()

    def csv_diagnosis_worker(self, path, label):
        report = diagnose_mt5_csv(path)
        text = format_csv_diagnosis(report)

        def show_result():
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
                    f"文字コード: {report['encoding']}"
                )
            else:
                problems = report.get("problems") or ["原因を特定できませんでした。"]
                details = "\n".join(f"・{item}" for item in problems)
                messagebox.showerror(
                    f"{label} CSV診断：読込不可",
                    "CSVを読み込めません。ログ欄に詳細を表示しました。\n\n"
                    + details[:1800]
                )

        self.after(0, show_result)

    def reload_plugins(self):
        try:
            loaded=load_plugins()
            self.write("プラグイン: "+(", ".join(loaded) if loaded else "なし"))
        except Exception as e:
            messagebox.showerror("プラグインエラー",str(e))

    def choose_preset(self):
        p=filedialog.askopenfilename(initialdir=app_dir()/"presets",filetypes=[("JSON","*.json")])
        if p:self.load_preset(Path(p))

    def load_preset(self,p):
        p = Path(p)
        try:
            cfg = load_settings_file(p)
            self.cfg = cfg
            self.vars["preset"].set(str(p))
            vals={"max_conditions":cfg.get("max_conditions",3),"max_candidates":cfg.get("max_candidates",300),
                  "min_trades":cfg.get("min_trades",100),"max_hold_bars":cfg.get("max_hold_bars",48),
                  "train_end":cfg.get("split_years",{}).get("train_end",2023),
                  "valid_end":cfg.get("split_years",{}).get("valid_end",2024)}
            for k,v in vals.items():
                self.inputs[k].set(str(v))
            self.write(f"設定JSON読込成功: {p}")
            self.write("settings_used.json をそのまま再利用できます。")
        except SettingsValidationError as e:
            details = "\n".join(f"・{x}" for x in e.errors)
            self.write("設定JSON読込失敗:")
            self.write(details)
            messagebox.showerror(
                "設定JSONを読み込めません",
                "次の問題を修正してください。\n\n" + details
            )
        except Exception:
            details = traceback.format_exc()
            self.write(details)
            messagebox.showerror("設定JSON読込エラー", details[-1800:])

    def sync_cfg(self):
        c=dict(self.cfg)
        field_labels = {
            "max_conditions":"最大条件数",
            "max_candidates":"最大候補数",
            "min_trades":"最低取引数",
            "max_hold_bars":"最大保有本数",
            "train_end":"学習終了年",
            "valid_end":"確認終了年",
        }
        parsed = {}
        gui_errors = []
        for k in ("max_conditions","max_candidates","min_trades","max_hold_bars","train_end","valid_end"):
            raw = self.inputs[k].get().strip()
            try:
                parsed[k] = int(raw)
            except ValueError:
                gui_errors.append(f"{field_labels[k]}: 整数で入力してください。現在値={raw!r}")
        if gui_errors:
            raise SettingsValidationError(gui_errors)

        for k in ("max_conditions","max_candidates","min_trades","max_hold_bars"):
            c[k]=parsed[k]
        c.setdefault("split_years",{})
        c["split_years"]["train_end"]=parsed["train_end"]
        c["split_years"]["valid_end"]=parsed["valid_end"]
        return validate_settings_dict(c)

    def save_preset(self):
        try:
            p=filedialog.asksaveasfilename(initialdir=app_dir()/"presets",defaultextension=".json",filetypes=[("JSON","*.json")])
            if p:
                Path(p).write_text(json.dumps(self.sync_cfg(),ensure_ascii=False,indent=2),encoding="utf-8")
                self.vars["preset"].set(p); self.write(f"プリセット保存: {p}")
        except SettingsValidationError as e:
            details="\n".join(f"・{x}" for x in e.errors)
            self.write("設定保存失敗:\n"+details)
            messagebox.showerror("設定を保存できません","次の問題を修正してください。\n\n"+details)
        except Exception as e:
            messagebox.showerror("保存エラー",str(e))

    def _format_seconds(self, seconds):
        if seconds is None or not isinstance(seconds, (int, float)) or seconds < 0:
            return "--"
        seconds = int(seconds)
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours}時間{minutes}分{secs}秒"
        if minutes:
            return f"{minutes}分{secs}秒"
        return f"{secs}秒"

    def _update_clock(self, remaining_seconds=None):
        if not self.run_started_at:
            return
        elapsed = time.time() - self.run_started_at
        self.elapsed_var.set(f"経過時間: {self._format_seconds(elapsed)}")
        if remaining_seconds is not None:
            self.remaining_var.set(
                f"残り時間: 約{self._format_seconds(remaining_seconds)}"
            )
            finish = datetime.fromtimestamp(time.time() + max(remaining_seconds, 0))
            self.finish_var.set(f"予想終了: {finish.strftime('%H:%M:%S')}")

    def set_stage(self, step, total, message, next_message="", stage_max=100):
        self.stage_started_at = time.time()
        self.stage_name = message
        self.stage_var.set(f"STEP {step}/{total}：{message}")
        self.stage_percent_var.set("0%")
        self.progress.configure(maximum=max(stage_max, 1), value=0)
        self.overall_progress.configure(
            value=max(0, min(100, ((step - 1) / total) * 100))
        )
        self.current_work_var.set(f"現在: {message}")
        self.next_work_var.set(f"次の処理: {next_message or '--'}")
        self.status.set(f"STEP {step}/{total}")
        self.write(
            f"[{datetime.now().strftime('%H:%M:%S')}] "
            f"STEP {step}/{total} | {message}"
        )
        self._update_clock()

    def update_stage_progress(
        self, step, total_steps, done, total, message,
        next_message="", overall_start=None, overall_end=None
    ):
        total = max(int(total), 1)
        done = max(0, min(int(done), total))
        fraction = done / total
        self.progress.configure(maximum=total, value=done)
        self.stage_percent_var.set(f"{fraction * 100:.1f}%")
        self.current_work_var.set(f"現在: {message}")
        if next_message:
            self.next_work_var.set(f"次の処理: {next_message}")

        if overall_start is None:
            overall_start = (step - 1) / total_steps
        if overall_end is None:
            overall_end = step / total_steps
        overall_fraction = overall_start + (overall_end - overall_start) * fraction
        self.overall_progress.configure(value=max(0, min(100, overall_fraction * 100)))

        stage_elapsed = max(time.time() - (self.stage_started_at or time.time()), 0.001)
        stage_rate = done / stage_elapsed if done > 0 else 0.0
        stage_remaining = (total - done) / stage_rate if stage_rate > 0 else None
        self._update_clock(stage_remaining)

    def request_cancel(self):
        if not self.run_started_at:
            return
        self.cancel_requested = True
        self.stop_btn.configure(state="disabled")
        self.status.set("中断要求を受付")
        self.current_work_var.set("現在: 安全に停止するため、現在の計算単位を終了中")
        self.next_work_var.set("次の処理: 中断")
        self.write("途中中断を要求しました。現在の計算単位が完了した時点で停止します。")

    def is_cancel_requested(self):
        return self.cancel_requested

    def update_search_progress(self, done, total, detail):
        elapsed = max(time.time() - self.stage_started_at, 0.001)
        rate = done / elapsed if done > 0 else 0.0
        remaining = (total - done) / rate if rate > 0 else None
        conditions = detail.get("conditions", "")
        side = detail.get("side", "")
        rr = detail.get("rr", "")
        stop_atr = detail.get("stop_atr", "")
        message = f"{conditions} / {side} / RR {rr} / SL ATR×{stop_atr}"

        self.progress.configure(maximum=total, value=done)
        self.stage_percent_var.set(f"{done / max(total,1) * 100:.1f}%")
        self.status.set(f"{done:,}/{total:,}")
        self.current_work_var.set(f"現在: {message}")
        self.next_work_var.set("次の処理: 候補ランキング作成 → 結果保存")
        self.remaining_var.set(
            f"残り時間: 約{self._format_seconds(remaining)} "
            f"（残り{max(total-done,0):,}件）"
        )
        if remaining is not None:
            finish = datetime.fromtimestamp(time.time() + remaining)
            self.finish_var.set(f"予想終了: {finish.strftime('%H:%M:%S')}")
        self.elapsed_var.set(
            f"経過時間: {self._format_seconds(time.time()-self.run_started_at)}"
        )
        self.overall_progress.configure(value=70 + 25 * done / max(total,1))

    def start(self):
        if not self.vars["ltf"].get() or not self.vars["out"].get():
            messagebox.showwarning("不足","下位足CSVと出力フォルダを選択してください"); return
        self.cancel_requested = False
        self.run_started_at = time.time()
        self.last_progress_time = self.run_started_at
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress["value"]=0
        self.overall_progress["value"]=0
        self.status.set("処理中")
        self.elapsed_var.set("経過時間: 0秒")
        self.remaining_var.set("残り時間: 計算中...")
        self.finish_var.set("予想終了: 計算中...")
        self.current_work_var.set("現在: 開始準備中")
        self.next_work_var.set("次の処理: 下位足CSV読込")
        threading.Thread(target=self.worker,daemon=True).start()

    def worker(self):
        total_steps = 9
        try:
            cfg = self.sync_cfg()

            self.after(0, lambda: self.set_stage(
                1,total_steps,"下位足CSVを読み込み中","下位足の特徴量生成"
            ))
            ltf, meta = read_mt5_csv(self.vars["ltf"].get())
            self.after(0, lambda m=meta: self.write(f"下位足読込完了: {m}"))
            if self.is_cancel_requested():
                raise InterruptedError("ユーザー操作により処理を中断しました。")

            self.after(0, lambda: self.set_stage(
                2,total_steps,"下位足の特徴量を生成中","上位足CSV読込",stage_max=10
            ))
            feature_counter = {"done": 0}
            def ltf_feature_log(message):
                feature_counter["done"] += 1
                self.after(0, lambda d=feature_counter["done"], msg=message:
                    self.update_stage_progress(
                        2,total_steps,d,10,msg,"上位足CSV読込",
                        overall_start=0.08,overall_end=0.25
                    ))
            feat = add_builtin_features(
                ltf,cfg,progress=ltf_feature_log,
                should_cancel=self.is_cancel_requested,label="下位足"
            )

            if self.vars["htf"].get():
                self.after(0, lambda: self.set_stage(
                    3,total_steps,"上位足CSVを読み込み中","上位足の特徴量生成"
                ))
                htf, hmeta = read_mt5_csv(self.vars["htf"].get())
                self.after(0, lambda m=hmeta: self.write(f"上位足読込完了: {m}"))

                self.after(0, lambda: self.set_stage(
                    4,total_steps,"上位足の特徴量を生成中","時間足同期",stage_max=10
                ))
                htf_counter = {"done": 0}
                def htf_feature_log(message):
                    htf_counter["done"] += 1
                    self.after(0, lambda d=htf_counter["done"], msg=message:
                        self.update_stage_progress(
                            4,total_steps,d,10,msg,"時間足同期",
                            overall_start=0.30,overall_end=0.42
                        ))
                hfeat = add_builtin_features(
                    htf,cfg,progress=htf_feature_log,
                    should_cancel=self.is_cancel_requested,label="上位足"
                )

                self.after(0, lambda: self.set_stage(
                    5,total_steps,"上位足と下位足を同期中","探索条件生成"
                ))
                feat = align_higher(feat,hfeat)
                self.after(0, lambda: self.write(f"時間足同期完了: {len(feat):,}本"))
            else:
                self.after(0, lambda: self.write("上位足処理をスキップしました。"))

            self.after(0, lambda: self.set_stage(
                6,total_steps,"探索条件を生成中","SL/TP結果の高速事前計算"
            ))

            def stage_progress(phase, done, total, message):
                if phase == "conditions":
                    step, next_msg = 6, "SL/TP結果の高速事前計算"
                    start_f, end_f = 0.50, 0.55
                else:
                    step, next_msg = 7, "候補条件の自動探索"
                    start_f, end_f = 0.55, 0.70
                    if done == 0:
                        self.after(0, lambda: self.set_stage(
                            7,total_steps,"SL/TP結果を高速事前計算中",
                            "候補条件の自動探索",stage_max=total
                        ))
                self.after(0, lambda s=step,d=done,t=total,msg=message,n=next_msg,a=start_f,b=end_f:
                    self.update_stage_progress(
                        s,total_steps,d,t,msg,n,
                        overall_start=a,overall_end=b
                    ))

            search_started = {"set": False}
            last_logged = {"done": 0}
            def prog(done,total,detail):
                if not search_started["set"]:
                    search_started["set"] = True
                    self.after(0, lambda: self.set_stage(
                        8,total_steps,"候補条件を自動探索中",
                        "ランキング作成・結果保存",stage_max=total
                    ))
                self.after(0, lambda d=done,t=total,x=detail:
                    self.update_search_progress(d,t,x))
                if done == total or done-last_logged["done"] >= 250:
                    last_logged["done"] = done
                    self.after(0, lambda d=done,t=total:
                        self.write(f"探索進捗: {d:,}/{t:,}"))

            ranking,_ = auto_discover(
                feat,cfg,progress=prog,
                should_cancel=self.is_cancel_requested,
                stage_progress=stage_progress
            )

            self.after(0, lambda: self.set_stage(
                9,total_steps,"ランキング作成・結果を保存中","完了"
            ))
            save_outputs(self.vars["out"].get(),ranking,feat,cfg)
            self.after(0, lambda: self.overall_progress.configure(value=100))
            self.after(0, lambda: self.progress.configure(maximum=1,value=1))
            self.after(0, lambda: self.stage_percent_var.set("100%"))
            self.after(0, lambda: self.current_work_var.set(
                f"現在: 完了（採用候補 {len(ranking):,}件）"
            ))
            self.after(0, lambda: self.next_work_var.set("次の処理: 結果CSVを確認"))
            self.after(0, lambda: self.remaining_var.set("残り時間: 0秒"))
            self.after(0, lambda: self.finish_var.set(
                f"終了時刻: {datetime.now().strftime('%H:%M:%S')}"
            ))
            self.after(0, lambda: self.write(
                f"完了: {self.vars['out'].get()} / 候補 {len(ranking):,}件"
            ))
            self.after(0, lambda: messagebox.showinfo(
                "完了",f"出力完了\n候補数: {len(ranking):,}"
            ))

        except InterruptedError as e:
            details=str(e)
            self.after(0,lambda:self.write(details))
            self.after(0,lambda:self.status.set("中断済み"))
            self.after(0,lambda:self.remaining_var.set("残り時間: 中断"))
            self.after(0,lambda:self.finish_var.set(
                f"中断時刻: {datetime.now().strftime('%H:%M:%S')}"
            ))
            self.after(0,lambda:self.current_work_var.set("現在: 中断されました"))
            self.after(0,lambda:self.next_work_var.set("次の処理: 設定を確認して再実行"))
            self.after(0,lambda:messagebox.showinfo("中断",details))
        except ValueError as e:
            details=str(e)
            self.after(0,lambda:self.write(details))
            self.after(0,lambda:messagebox.showerror(
                "CSVまたは入力データのエラー",details[-1800:]
            ))
        except Exception:
            err=traceback.format_exc()
            self.after(0,lambda:self.write(err))
            self.after(0,lambda:messagebox.showerror("エラー",err[-1800:]))
        finally:
            def finish_ui():
                self.run_btn.configure(state="normal")
                self.stop_btn.configure(state="disabled")
                if self.status.get() not in ("中断済み",):
                    self.status.set("完了")
                self.run_started_at=None
            self.after(0,finish_ui)
