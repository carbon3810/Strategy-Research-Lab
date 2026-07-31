
from __future__ import annotations
import csv, json, math, re, importlib.util, sys
from pathlib import Path
from itertools import combinations, product
import numpy as np
import pandas as pd
from plugin_api import registry

OHLC = ["time","open","high","low","close","tick_volume","volume","spread"]

def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

def bundled_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", app_dir()))

def ensure_external_assets():
    base = app_dir()
    for name in ("plugins", "presets"):
        target = base/name
        if not target.exists():
            src = bundled_dir()/name
            if src.exists():
                import shutil
                shutil.copytree(src, target)

def load_plugins() -> list[str]:
    ensure_external_assets()
    loaded, errors = [], []
    paths = [app_dir()/"plugins"/"indicators", app_dir()/"plugins"/"signals"]
    for folder in paths:
        folder.mkdir(parents=True, exist_ok=True)
        for p in sorted(folder.glob("*.py")):
            if p.name.startswith("_"):
                continue
            try:
                module_name = "srl_plugin_" + re.sub(r"\W+", "_", str(p))
                spec = importlib.util.spec_from_file_location(module_name, p)
                mod = importlib.util.module_from_spec(spec)
                assert spec and spec.loader
                spec.loader.exec_module(mod)
                loaded.append(p.name)
            except Exception as e:
                errors.append(f"{p.name}: {e}")
    if errors:
        raise RuntimeError("プラグイン読込エラー\n" + "\n".join(errors))
    return loaded

def _decode(path: Path) -> tuple[str,str]:
    raw = path.read_bytes()
    for enc in ("utf-16","utf-8-sig","cp932","utf-8"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            pass
    raise ValueError("文字コードを判定できません")

def read_mt5_csv(path: str) -> tuple[pd.DataFrame, dict]:
    p = Path(path)
    text, enc = _decode(p)
    sample = text[:5000]
    delim = "\t" if sample.count("\t") > sample.count(",") else ","
    rows = list(csv.reader(text.splitlines(), delimiter=delim))
    rows = [r for r in rows if r and any(str(x).strip() for x in r)]
    if not rows:
        raise ValueError("CSVが空です")
    first = [str(x).strip().lower().replace("<","").replace(">","") for x in rows[0]]
    has_header = any(x in first for x in ("date","time","open","high","low","close"))
    data = rows[1:] if has_header else rows
    if has_header:
        cols = first
        df = pd.DataFrame(data, columns=cols[:len(data[0])])
        if "date" in df and "time" in df:
            dt = df["date"].astype(str)+" "+df["time"].astype(str)
        elif "time" in df:
            dt = df["time"].astype(str)
        else:
            dt = df.iloc[:,0].astype(str)
        mapping = {}
        for target in ("open","high","low","close","tick_volume","volume","spread"):
            if target in df.columns: mapping[target] = target
        out = pd.DataFrame({"time": pd.to_datetime(dt, errors="coerce")})
        for target, source in mapping.items():
            out[target] = pd.to_numeric(df[source], errors="coerce")
    else:
        if len(data[0]) < 6:
            raise ValueError("MT5形式として列数が不足しています")
        # Standard MT5: date, time, open, high, low, close, tickvol, vol, spread
        out = pd.DataFrame()
        out["time"] = pd.to_datetime(pd.Series([r[0]+" "+r[1] for r in data]), errors="coerce")
        names = ["open","high","low","close","tick_volume","volume","spread"]
        for i, name in enumerate(names, start=2):
            if i < max(map(len,data)):
                out[name] = pd.to_numeric(pd.Series([r[i] if i < len(r) else None for r in data]), errors="coerce")
    out = out.dropna(subset=["time","open","high","low","close"]).sort_values("time").drop_duplicates("time").reset_index(drop=True)
    if len(out) < 100:
        raise ValueError("有効なローソク足が100本未満です")
    return out, {"encoding":enc,"delimiter":"tab" if delim=="\t" else "comma","bars":len(out),
                 "first":str(out.time.iloc[0]),"last":str(out.time.iloc[-1])}

def add_builtin_features(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    x = df.copy()
    periods = sorted(set(cfg.get("ema_periods",[20,50,200])))
    for n in periods:
        x[f"ema_{n}"] = x.close.ewm(span=n, adjust=False).mean()
        x[f"above_ema_{n}"] = x.close > x[f"ema_{n}"]
        x[f"ema_{n}_slope"] = x[f"ema_{n}"].pct_change(cfg.get("slope_lookback",5))
    tr = pd.concat([(x.high-x.low),(x.high-x.close.shift()).abs(),(x.low-x.close.shift()).abs()],axis=1).max(axis=1)
    atr_n = int(cfg.get("atr_period",14))
    x["atr"] = tr.ewm(alpha=1/atr_n, adjust=False).mean()
    x["body"] = (x.close-x.open).abs()
    x["range"] = (x.high-x.low).replace(0,np.nan)
    x["body_ratio"] = x.body/x.range
    x["upper_wick_ratio"] = (x.high-x[["open","close"]].max(axis=1))/x.range
    x["lower_wick_ratio"] = (x[["open","close"]].min(axis=1)-x.low)/x.range
    x["bull"] = x.close > x.open
    x["bear"] = x.close < x.open
    x["bull_engulf"] = x.bull & x.open.le(x.close.shift()) & x.close.ge(x.open.shift()) & x.body.gt(x.body.shift())
    x["bear_engulf"] = x.bear & x.open.ge(x.close.shift()) & x.close.le(x.open.shift()) & x.body.gt(x.body.shift())
    look = int(cfg.get("breakout_lookback",20))
    x["high_breakout"] = x.close > x.high.shift(1).rolling(look).max()
    x["low_breakout"] = x.close < x.low.shift(1).rolling(look).min()
    x["hour"] = x.time.dt.hour
    x["weekday"] = x.time.dt.weekday
    x["atr_pct"] = x.atr/x.close
    x["volatility_high"] = x.atr_pct > x.atr_pct.rolling(200).median()
    for name, func in registry.indicators.items():
        result = func(x, cfg.get("plugin_params",{}).get(name,{}))
        if isinstance(result, pd.Series):
            x[name] = result
        elif isinstance(result, pd.DataFrame):
            for c in result.columns: x[c] = result[c]
    return x

def align_higher(lower: pd.DataFrame, higher: pd.DataFrame) -> pd.DataFrame:
    h = higher.copy()
    # shift one higher bar to use only completed bar information
    feature_cols = [c for c in h.columns if c != "time"]
    h[feature_cols] = h[feature_cols].shift(1)
    h = h.rename(columns={c:"HTF_"+c for c in feature_cols})
    return pd.merge_asof(lower.sort_values("time"), h.sort_values("time"), on="time", direction="backward")

def build_conditions(df: pd.DataFrame, cfg: dict) -> dict[str,pd.Series]:
    cond = {}
    for c in df.columns:
        if df[c].dtype == bool:
            cond[c] = df[c].fillna(False)
    for n in cfg.get("ema_periods",[20,50,200]):
        if f"above_ema_{n}" in df: cond[f"価格>EMA{n}"] = df[f"above_ema_{n}"].fillna(False)
        if f"HTF_above_ema_{n}" in df: cond[f"上位足_価格>EMA{n}"] = df[f"HTF_above_ema_{n}"].fillna(False)
        if f"ema_{n}_slope" in df:
            cond[f"EMA{n}_上向き"] = df[f"ema_{n}_slope"] > 0
            cond[f"EMA{n}_下向き"] = df[f"ema_{n}_slope"] < 0
        if f"HTF_ema_{n}_slope" in df:
            cond[f"上位足_EMA{n}_上向き"] = df[f"HTF_ema_{n}_slope"] > 0
            cond[f"上位足_EMA{n}_下向き"] = df[f"HTF_ema_{n}_slope"] < 0
    cond["ロンドン時間"] = df.hour.between(15,22)
    cond["東京時間"] = df.hour.between(8,14)
    cond["NY時間"] = (df.hour >= 21) | (df.hour <= 3)
    for name, func in registry.signals.items():
        cond[name] = func(df, cfg.get("plugin_params",{}).get(name,{})).fillna(False).astype(bool)
    return cond

def simulate(df: pd.DataFrame, entry_mask: pd.Series, side: str, rr: float, stop_mult: float, max_hold: int) -> tuple[dict,list]:
    idxs = np.flatnonzero(entry_mask.to_numpy())
    results=[]
    for i in idxs:
        if i+1 >= len(df): continue
        entry_i=i+1
        entry=float(df.open.iloc[entry_i])
        atr=float(df.atr.iloc[i]) if pd.notna(df.atr.iloc[i]) else math.nan
        if not math.isfinite(atr) or atr <= 0: continue
        risk=atr*stop_mult
        if side=="BUY": sl, tp = entry-risk, entry+risk*rr
        else: sl, tp = entry+risk, entry-risk*rr
        exit_r=0.0; exit_i=min(entry_i+max_hold,len(df)-1); reason="TIME"
        for j in range(entry_i, min(entry_i+max_hold+1,len(df))):
            hi,lo=float(df.high.iloc[j]),float(df.low.iloc[j])
            hit_sl = lo<=sl if side=="BUY" else hi>=sl
            hit_tp = hi>=tp if side=="BUY" else lo<=tp
            if hit_sl and hit_tp:
                exit_r=-1.0; exit_i=j; reason="SL_samebar"; break
            if hit_sl:
                exit_r=-1.0; exit_i=j; reason="SL"; break
            if hit_tp:
                exit_r=rr; exit_i=j; reason="TP"; break
        else:
            close=float(df.close.iloc[exit_i])
            exit_r=(close-entry)/risk if side=="BUY" else (entry-close)/risk
        results.append({"entry_time":df.time.iloc[entry_i],"exit_time":df.time.iloc[exit_i],
                        "side":side,"entry":entry,"sl":sl,"tp":tp,"R":exit_r,"reason":reason})
    if not results:
        return {"trades":0,"wins":0,"win_rate":0,"pf":0,"total_r":0,"max_dd_r":0}, []
    r=np.array([z["R"] for z in results],dtype=float)
    gross_win=r[r>0].sum(); gross_loss=-r[r<0].sum()
    pf=float(gross_win/gross_loss) if gross_loss>0 else float("inf")
    eq=np.cumsum(r); peak=np.maximum.accumulate(np.r_[0,eq]); dd=peak[1:]-eq
    stats={"trades":len(r),"wins":int((r>0).sum()),"win_rate":float((r>0).mean()*100),
           "pf":pf,"total_r":float(r.sum()),"avg_r":float(r.mean()),"max_dd_r":float(dd.max() if len(dd) else 0)}
    return stats,results

def auto_discover(df: pd.DataFrame, cfg: dict, progress=None):
    conds=build_conditions(df,cfg)
    names=sorted(conds)
    max_k=int(cfg.get("max_conditions",3))
    min_trades=int(cfg.get("min_trades",100))
    max_candidates=int(cfg.get("max_candidates",300))
    rr_values=cfg.get("rr_values",[1.0,1.5,2.0])
    stop_values=cfg.get("stop_mult_values",[1.0])
    sides=cfg.get("sides",["BUY","SELL"])
    combos=[]
    for k in range(1,max_k+1):
        for c in combinations(names,k):
            combos.append(c)
            if len(combos)>=max_candidates: break
        if len(combos)>=max_candidates: break
    split=cfg.get("split_years",{})
    train_end=int(split.get("train_end",2023)); valid_end=int(split.get("valid_end",2024))
    periods={"train":df.time.dt.year<=train_end,
             "valid":(df.time.dt.year>train_end)&(df.time.dt.year<=valid_end),
             "oos":df.time.dt.year>valid_end}
    ranking=[]; trades_top=[]
    total=max(1,len(combos)*len(rr_values)*len(stop_values)*len(sides)); done=0
    for combo in combos:
        mask=pd.Series(True,index=df.index)
        for c in combo: mask &= conds[c]
        for side,rr,sm in product(sides,rr_values,stop_values):
            row={"conditions":" AND ".join(combo),"side":side,"rr":rr,"stop_atr":sm}
            okay=True
            all_period_trades=[]
            for pname,pmask in periods.items():
                st,tr=simulate(df,mask&pmask,side,float(rr),float(sm),int(cfg.get("max_hold_bars",48)))
                for key,val in st.items(): row[f"{pname}_{key}"]=val
                if pname=="train" and st["trades"]<min_trades: okay=False
                if pname=="oos": all_period_trades=tr
            # Stability-oriented score, not PF-only
            def finite_pf(v): return min(float(v),5.0) if math.isfinite(float(v)) else 5.0
            row["score"]=(finite_pf(row["valid_pf"])*0.35 + finite_pf(row["oos_pf"])*0.45 +
                          min(row["oos_trades"]/max(min_trades,1),2)*0.10 -
                          min(row["oos_max_dd_r"]/100,2)*0.10)
            if okay: ranking.append(row)
            done+=1
            if progress and done%10==0: progress(done,total)
    ranking=sorted(ranking,key=lambda z:z["score"],reverse=True)
    return pd.DataFrame(ranking), conds

def save_outputs(out_dir: str, ranking: pd.DataFrame, features: pd.DataFrame, cfg: dict):
    p=Path(out_dir); p.mkdir(parents=True,exist_ok=True)
    ranking.to_csv(p/"strategy_ranking.csv",index=False,encoding="utf-8-sig")
    keep=["time","open","high","low","close"]+[c for c in features.columns if c not in ("time","open","high","low","close")][:100]
    features[keep].to_csv(p/"features.csv",index=False,encoding="utf-8-sig")
    (p/"settings_used.json").write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding="utf-8")
    lines=["Strategy Research Lab 自動探索レポート","",f"候補数: {len(ranking):,}",""]
    for i,row in ranking.head(20).iterrows():
        lines.append(f"{i+1}. {row['conditions']} / {row['side']} RR{row['rr']}")
        lines.append(f"   Train PF {row['train_pf']:.2f}, Valid PF {row['valid_pf']:.2f}, OOS PF {row['oos_pf']:.2f}, OOS取引 {int(row['oos_trades'])}, OOS DD {row['oos_max_dd_r']:.1f}R")
    (p/"research_report.txt").write_text("\n".join(lines),encoding="utf-8")
