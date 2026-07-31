
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

def _decode(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    if not raw:
        raise ValueError("CSVファイルが空です。")

    attempts = []
    for enc in ("utf-16", "utf-8-sig", "cp932", "utf-8"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError as exc:
            attempts.append(f"{enc}: byte {exc.start}")

    raise ValueError(
        "文字コードを判定できません。対応文字コードは UTF-16、UTF-8、CP932 です。\n"
        + "\n".join(attempts)
    )


def _normalize_header(value: str) -> str:
    return (
        str(value)
        .strip()
        .lower()
        .replace("<", "")
        .replace(">", "")
        .replace(" ", "_")
    )


def _parse_datetime(values: pd.Series) -> pd.Series:
    """MT5でよく使われる日時形式を段階的に解析する。"""
    text = values.astype(str).str.strip()
    result = pd.Series(pd.NaT, index=text.index, dtype="datetime64[ns]")

    formats = (
        "%Y.%m.%d %H:%M:%S",
        "%Y.%m.%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    )
    for fmt in formats:
        missing = result.isna()
        if not missing.any():
            break
        result.loc[missing] = pd.to_datetime(
            text.loc[missing], format=fmt, errors="coerce"
        )

    missing = result.isna()
    if missing.any():
        result.loc[missing] = pd.to_datetime(
            text.loc[missing], errors="coerce", dayfirst=False
        )
    return result


def _infer_timeframe(times: pd.Series) -> str:
    diffs = times.sort_values().diff().dropna()
    diffs = diffs[diffs > pd.Timedelta(0)]
    if diffs.empty:
        return "判定不能"

    seconds = int(diffs.dt.total_seconds().median())
    mapping = {
        60: "M1",
        300: "M5",
        900: "M15",
        1800: "M30",
        3600: "H1",
        14400: "H4",
        86400: "D1",
        604800: "W1",
    }
    if seconds in mapping:
        return mapping[seconds]
    if seconds % 86400 == 0:
        return f"D{seconds // 86400}"
    if seconds % 3600 == 0:
        return f"H{seconds // 3600}"
    if seconds % 60 == 0:
        return f"M{seconds // 60}"
    return f"{seconds}秒"


def diagnose_mt5_csv(path: str) -> dict:
    """
    CSVを解析し、読込可否と失敗箇所を詳細に返す。
    この関数自体は、可能な限り例外を外へ出さず診断結果に格納する。
    """
    report = {
        "ok": False,
        "file": str(path),
        "file_name": Path(path).name if path else "",
        "file_size_bytes": 0,
        "encoding": "不明",
        "delimiter": "不明",
        "header": "不明",
        "layout": "不明",
        "total_rows": 0,
        "column_count": 0,
        "valid_bars": 0,
        "invalid_rows": 0,
        "invalid_datetime": 0,
        "invalid_open": 0,
        "invalid_high": 0,
        "invalid_low": 0,
        "invalid_close": 0,
        "duplicate_times": 0,
        "first": "",
        "last": "",
        "timeframe": "判定不能",
        "sample_rows": [],
        "problems": [],
        "warnings": [],
    }

    try:
        p = Path(path)
        if not path:
            report["problems"].append("CSVファイルが指定されていません。")
            return report
        if not p.exists():
            report["problems"].append(f"ファイルが見つかりません: {p}")
            return report
        if not p.is_file():
            report["problems"].append(f"ファイルではありません: {p}")
            return report

        report["file_size_bytes"] = p.stat().st_size
        text, enc = _decode(p)
        report["encoding"] = enc

        sample = text[:10000]
        delimiter_counts = {
            "タブ": sample.count("\t"),
            "カンマ": sample.count(","),
            "セミコロン": sample.count(";"),
        }
        delimiter_name = max(delimiter_counts, key=delimiter_counts.get)
        delimiter = {"タブ": "\t", "カンマ": ",", "セミコロン": ";"}[delimiter_name]
        report["delimiter"] = delimiter_name

        rows = list(csv.reader(text.splitlines(), delimiter=delimiter))
        rows = [row for row in rows if row and any(str(v).strip() for v in row)]
        report["total_rows"] = len(rows)

        if not rows:
            report["problems"].append("CSVに有効な行がありません。")
            return report

        report["column_count"] = max(len(row) for row in rows)
        report["sample_rows"] = rows[:3]

        first_norm = [_normalize_header(v) for v in rows[0]]
        header_tokens = {
            "date", "time", "datetime", "open", "high", "low", "close",
            "tickvol", "tick_volume", "volume", "spread"
        }
        has_header = any(v in header_tokens for v in first_norm)
        report["header"] = "あり" if has_header else "なし"

        data = rows[1:] if has_header else rows
        if not data:
            report["problems"].append("ヘッダー以外のデータ行がありません。")
            return report

        lengths = pd.Series([len(row) for row in data])
        common_columns = int(lengths.mode().iloc[0])
        irregular_count = int((lengths != common_columns).sum())
        if irregular_count:
            report["warnings"].append(
                f"列数が揃っていない行が {irregular_count:,} 行あります。"
                f" 最頻列数は {common_columns} 列です。"
            )

        raw = pd.DataFrame(data)
        out = pd.DataFrame(index=raw.index)

        if has_header:
            columns = first_norm
            if len(columns) < raw.shape[1]:
                columns += [f"unknown_{i}" for i in range(len(columns), raw.shape[1])]
            raw.columns = columns[:raw.shape[1]]

            if "date" in raw.columns and "time" in raw.columns:
                dt_text = raw["date"].astype(str) + " " + raw["time"].astype(str)
                report["layout"] = "ヘッダーあり・日付列と時刻列が別"
            elif "datetime" in raw.columns:
                dt_text = raw["datetime"]
                report["layout"] = "ヘッダーあり・日時1列"
            elif "time" in raw.columns:
                dt_text = raw["time"]
                report["layout"] = "ヘッダーあり・日時1列"
            else:
                dt_text = raw.iloc[:, 0]
                report["layout"] = "ヘッダーあり・先頭列を日時として推定"
                report["warnings"].append(
                    "DATE/TIME列名を認識できなかったため、先頭列を日時として使用しました。"
                )

            aliases = {
                "open": ("open",),
                "high": ("high",),
                "low": ("low",),
                "close": ("close",),
                "tick_volume": ("tick_volume", "tickvol", "tickvolume"),
                "volume": ("volume", "vol"),
                "spread": ("spread",),
            }
            source_columns = {}
            for target, candidates in aliases.items():
                for candidate in candidates:
                    if candidate in raw.columns:
                        source_columns[target] = candidate
                        break

            missing = [name for name in ("open", "high", "low", "close")
                       if name not in source_columns]
            if missing:
                report["problems"].append(
                    "必須列を認識できません: " + ", ".join(missing)
                )
                report["problems"].append(
                    "認識した列名: " + ", ".join(map(str, raw.columns))
                )
                return report

            out["time"] = _parse_datetime(dt_text)
            for target, source in source_columns.items():
                out[target] = pd.to_numeric(raw[source], errors="coerce")

        else:
            # MT5の代表的な2形式を自動判別する。
            # 形式A: 日時, OPEN, HIGH, LOW, CLOSE, TICKVOL, VOL[, SPREAD]
            # 形式B: DATE, TIME, OPEN, HIGH, LOW, CLOSE, TICKVOL, VOL[, SPREAD]
            first_value = raw.iloc[:, 0].astype(str)
            first_has_time = first_value.str.contains(
                r"\d{1,2}:\d{2}", regex=True, na=False
            ).mean() > 0.5

            if first_has_time and raw.shape[1] >= 5:
                report["layout"] = "ヘッダーなし・日時1列"
                dt_text = raw.iloc[:, 0]
                price_start = 1
            elif raw.shape[1] >= 6:
                report["layout"] = "ヘッダーなし・日付列と時刻列が別"
                dt_text = raw.iloc[:, 0].astype(str) + " " + raw.iloc[:, 1].astype(str)
                price_start = 2
            else:
                report["problems"].append(
                    f"MT5形式として列数が不足しています。検出列数={raw.shape[1]}"
                )
                report["problems"].append(
                    "必要形式: 日時,OPEN,HIGH,LOW,CLOSE,... または "
                    "DATE,TIME,OPEN,HIGH,LOW,CLOSE,..."
                )
                return report

            if raw.shape[1] < price_start + 4:
                report["problems"].append(
                    f"OHLC列が不足しています。検出列数={raw.shape[1]}"
                )
                return report

            out["time"] = _parse_datetime(dt_text)
            names = ["open", "high", "low", "close",
                     "tick_volume", "volume", "spread"]
            for offset, name in enumerate(names):
                column_index = price_start + offset
                if column_index < raw.shape[1]:
                    out[name] = pd.to_numeric(raw.iloc[:, column_index], errors="coerce")

        required = ("time", "open", "high", "low", "close")
        for column in required:
            if column not in out.columns:
                report["problems"].append(f"内部解析で {column} 列を生成できませんでした。")
                return report

        report["invalid_datetime"] = int(out["time"].isna().sum())
        report["invalid_open"] = int(out["open"].isna().sum())
        report["invalid_high"] = int(out["high"].isna().sum())
        report["invalid_low"] = int(out["low"].isna().sum())
        report["invalid_close"] = int(out["close"].isna().sum())

        valid_mask = out[list(required)].notna().all(axis=1)
        valid = out.loc[valid_mask].copy()
        report["valid_bars"] = len(valid)
        report["invalid_rows"] = len(out) - len(valid)

        if not valid.empty:
            report["duplicate_times"] = int(valid["time"].duplicated().sum())
            valid = (
                valid.sort_values("time")
                .drop_duplicates("time")
                .reset_index(drop=True)
            )
            report["first"] = str(valid["time"].iloc[0])
            report["last"] = str(valid["time"].iloc[-1])
            report["timeframe"] = _infer_timeframe(valid["time"])

            impossible_ohlc = (
                (valid["high"] < valid[["open", "close", "low"]].max(axis=1))
                | (valid["low"] > valid[["open", "close", "high"]].min(axis=1))
            )
            impossible_count = int(impossible_ohlc.sum())
            if impossible_count:
                report["warnings"].append(
                    f"OHLC関係が不自然な行が {impossible_count:,} 行あります。"
                )

        if report["invalid_datetime"]:
            report["problems"].append(
                f"日時を解析できない行が {report['invalid_datetime']:,} 行あります。"
            )
        for label, key in (
            ("OPEN", "invalid_open"),
            ("HIGH", "invalid_high"),
            ("LOW", "invalid_low"),
            ("CLOSE", "invalid_close"),
        ):
            if report[key]:
                report["problems"].append(
                    f"{label}を数値として読めない行が {report[key]:,} 行あります。"
                )

        if report["valid_bars"] < 100:
            report["problems"].append(
                f"有効なローソク足が100本未満です。"
                f" 有効={report['valid_bars']:,} / 総データ={len(out):,}"
            )
        else:
            report["ok"] = True

        return report

    except Exception as exc:
        report["problems"].append(
            f"{type(exc).__name__}: {exc}"
        )
        return report


def format_csv_diagnosis(report: dict) -> str:
    status = "読込可能" if report.get("ok") else "読込不可"
    size_mb = report.get("file_size_bytes", 0) / 1024 / 1024
    lines = [
        "=" * 58,
        "CSV診断結果",
        "=" * 58,
        f"状態             : {status}",
        f"ファイル         : {report.get('file', '')}",
        f"ファイルサイズ   : {size_mb:.2f} MB",
        f"文字コード       : {report.get('encoding', '不明')}",
        f"区切り文字       : {report.get('delimiter', '不明')}",
        f"ヘッダー         : {report.get('header', '不明')}",
        f"認識形式         : {report.get('layout', '不明')}",
        f"検出列数         : {report.get('column_count', 0):,}",
        f"総行数           : {report.get('total_rows', 0):,}",
        f"有効ローソク足   : {report.get('valid_bars', 0):,}",
        f"無効行           : {report.get('invalid_rows', 0):,}",
        f"日時NG           : {report.get('invalid_datetime', 0):,}",
        f"OPEN NG          : {report.get('invalid_open', 0):,}",
        f"HIGH NG          : {report.get('invalid_high', 0):,}",
        f"LOW NG           : {report.get('invalid_low', 0):,}",
        f"CLOSE NG         : {report.get('invalid_close', 0):,}",
        f"重複日時         : {report.get('duplicate_times', 0):,}",
        f"推定時間足       : {report.get('timeframe', '判定不能')}",
        f"開始日時         : {report.get('first', '')}",
        f"終了日時         : {report.get('last', '')}",
    ]

    samples = report.get("sample_rows") or []
    if samples:
        lines.extend(["", "先頭行サンプル:"])
        for index, row in enumerate(samples, start=1):
            preview = " | ".join(map(str, row))
            if len(preview) > 220:
                preview = preview[:217] + "..."
            lines.append(f"  {index}: {preview}")

    warnings = report.get("warnings") or []
    if warnings:
        lines.extend(["", "警告:"])
        lines.extend(f"  ・{item}" for item in warnings)

    problems = report.get("problems") or []
    if problems:
        lines.extend(["", "問題点:"])
        lines.extend(f"  ・{item}" for item in problems)

    lines.append("=" * 58)
    return "\n".join(lines)


def read_mt5_csv(path: str) -> tuple[pd.DataFrame, dict]:
    report = diagnose_mt5_csv(path)
    if not report["ok"]:
        raise ValueError(format_csv_diagnosis(report))

    p = Path(path)
    text, _ = _decode(p)
    delimiter = {"タブ": "\t", "カンマ": ",", "セミコロン": ";"}.get(
        report["delimiter"], ","
    )
    rows = list(csv.reader(text.splitlines(), delimiter=delimiter))
    rows = [row for row in rows if row and any(str(v).strip() for v in row)]

    first_norm = [_normalize_header(v) for v in rows[0]]
    header_tokens = {
        "date", "time", "datetime", "open", "high", "low", "close",
        "tickvol", "tick_volume", "volume", "spread"
    }
    has_header = any(v in header_tokens for v in first_norm)
    data = rows[1:] if has_header else rows
    raw = pd.DataFrame(data)
    out = pd.DataFrame(index=raw.index)

    if has_header:
        columns = first_norm
        if len(columns) < raw.shape[1]:
            columns += [f"unknown_{i}" for i in range(len(columns), raw.shape[1])]
        raw.columns = columns[:raw.shape[1]]

        if "date" in raw.columns and "time" in raw.columns:
            dt_text = raw["date"].astype(str) + " " + raw["time"].astype(str)
        elif "datetime" in raw.columns:
            dt_text = raw["datetime"]
        elif "time" in raw.columns:
            dt_text = raw["time"]
        else:
            dt_text = raw.iloc[:, 0]

        aliases = {
            "open": ("open",),
            "high": ("high",),
            "low": ("low",),
            "close": ("close",),
            "tick_volume": ("tick_volume", "tickvol", "tickvolume"),
            "volume": ("volume", "vol"),
            "spread": ("spread",),
        }
        out["time"] = _parse_datetime(dt_text)
        for target, candidates in aliases.items():
            for candidate in candidates:
                if candidate in raw.columns:
                    out[target] = pd.to_numeric(raw[candidate], errors="coerce")
                    break
    else:
        first_has_time = raw.iloc[:, 0].astype(str).str.contains(
            r"\d{1,2}:\d{2}", regex=True, na=False
        ).mean() > 0.5
        if first_has_time:
            dt_text = raw.iloc[:, 0]
            price_start = 1
        else:
            dt_text = raw.iloc[:, 0].astype(str) + " " + raw.iloc[:, 1].astype(str)
            price_start = 2

        out["time"] = _parse_datetime(dt_text)
        names = ["open", "high", "low", "close",
                 "tick_volume", "volume", "spread"]
        for offset, name in enumerate(names):
            column_index = price_start + offset
            if column_index < raw.shape[1]:
                out[name] = pd.to_numeric(raw.iloc[:, column_index], errors="coerce")

    out = (
        out.dropna(subset=["time", "open", "high", "low", "close"])
        .sort_values("time")
        .drop_duplicates("time")
        .reset_index(drop=True)
    )

    meta = {
        "encoding": report["encoding"],
        "delimiter": report["delimiter"],
        "layout": report["layout"],
        "bars": len(out),
        "first": str(out["time"].iloc[0]),
        "last": str(out["time"].iloc[-1]),
        "timeframe": report["timeframe"],
    }
    return out, meta


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

def auto_discover(df: pd.DataFrame, cfg: dict, progress=None, should_cancel=None):
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
        if should_cancel and should_cancel():
            raise InterruptedError("ユーザー操作により自動探索を中断しました。")
        mask=pd.Series(True,index=df.index)
        for c in combo: mask &= conds[c]
        for side,rr,sm in product(sides,rr_values,stop_values):
            if should_cancel and should_cancel():
                raise InterruptedError("ユーザー操作により自動探索を中断しました。")
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
            done += 1
            if progress and (done % 10 == 0 or done == total):
                detail = {
                    "conditions": " AND ".join(combo),
                    "side": side,
                    "rr": rr,
                    "stop_atr": sm,
                }
                progress(done, total, detail)
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
