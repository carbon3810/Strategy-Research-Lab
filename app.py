
from __future__ import annotations
import json, threading, traceback
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from core import app_dir, ensure_external_assets, load_plugins, read_mt5_csv, add_builtin_features, align_higher, auto_discover, save_outputs

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
        self.geometry("950x720")
        self.minsize(850,650)
        ensure_external_assets()
        self.cfg={}
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
        self._file_row(files,0,"上位足CSV",self.vars["htf"])
        self._file_row(files,1,"下位足CSV",self.vars["ltf"])
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

        action=ttk.Frame(self); action.pack(fill="x",padx=12,pady=8)
        self.run_btn=ttk.Button(action,text="自動探索を開始",command=self.start); self.run_btn.pack(side="left")
        self.progress=ttk.Progressbar(action,mode="determinate"); self.progress.pack(side="left",fill="x",expand=True,padx=10)
        self.status=tk.StringVar(value="準備完了")
        ttk.Label(action,textvariable=self.status).pack(side="right")

        logf=ttk.LabelFrame(self,text="ログ"); logf.pack(fill="both",expand=True,padx=12,pady=6)
        self.log=tk.Text(logf,wrap="word",height=18); self.log.pack(fill="both",expand=True,padx=6,pady=6)

        note=ttk.Label(self,text="新しい指標・シグナルはEXEと同じ場所の plugins フォルダへ追加します。設定値はpresetで変更できます。",
                       foreground="#555")
        note.pack(fill="x",padx=14,pady=(0,8))

    def _file_row(self,parent,row,label,var):
        ttk.Label(parent,text=label).grid(row=row,column=0,sticky="w",padx=8,pady=6)
        ttk.Entry(parent,textvariable=var).grid(row=row,column=1,sticky="ew",padx=8,pady=6)
        ttk.Button(parent,text="選択",command=lambda:self.pick_file(var)).grid(row=row,column=2,padx=8,pady=6)
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

    def start(self):
        if not self.vars["ltf"].get() or not self.vars["out"].get():
            messagebox.showwarning("不足","下位足CSVと出力フォルダを選択してください"); return
        self.run_btn.configure(state="disabled"); self.progress["value"]=0; self.status.set("処理中")
        threading.Thread(target=self.worker,daemon=True).start()

    def worker(self):
        try:
            cfg=self.sync_cfg()
            self.after(0,lambda:self.write("下位足CSVを読込中..."))
            ltf,meta=read_mt5_csv(self.vars["ltf"].get())
            self.after(0,lambda:self.write(f"下位足: {meta}"))
            feat=add_builtin_features(ltf,cfg)
            if self.vars["htf"].get():
                htf,hmeta=read_mt5_csv(self.vars["htf"].get())
                self.after(0,lambda:self.write(f"上位足: {hmeta}"))
                hfeat=add_builtin_features(htf,cfg)
                feat=align_higher(feat,hfeat)
            def prog(done,total):
                self.after(0,lambda d=done,t=total:(self.progress.configure(maximum=t,value=d),self.status.set(f"{d}/{t}")))
            ranking,_=auto_discover(feat,cfg,prog)
            save_outputs(self.vars["out"].get(),ranking,feat,cfg)
            self.after(0,lambda:self.write(f"完了: {len(ranking):,}候補を保存"))
            self.after(0,lambda:messagebox.showinfo("完了","自動探索が完了しました。\nstrategy_ranking.csv をGPTへ渡して研究を続けてください。"))
        except SettingsValidationError as e:
            details="\n".join(f"・{x}" for x in e.errors)
            self.after(0,lambda:self.write("設定エラー:\n"+details))
            self.after(0,lambda:messagebox.showerror("設定エラー","次の設定を修正してください。\n\n"+details))
        except Exception:
            err=traceback.format_exc()
            self.after(0,lambda:self.write(err))
            self.after(0,lambda:messagebox.showerror("エラー",err[-1500:]))
        finally:
            self.after(0,lambda:(self.run_btn.configure(state="normal"),self.status.set("完了")))

if __name__=="__main__":
    App().mainloop()
