from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Callable

from core import (
    add_builtin_features,
    align_higher,
    auto_discover,
    read_mt5_csv,
    save_outputs,
)

ProgressCallback = Callable[[str, int, int, dict[str, Any]], None]
LogCallback = Callable[[str], None]


@dataclass(frozen=True)
class ResearchRequest:
    lower_csv: str
    higher_csv: str
    output_dir: str
    settings: dict[str, Any]


@dataclass(frozen=True)
class ResearchResult:
    candidate_count: int
    output_dir: str


class ResearchController:
    """GUIと研究エンジン(core.py)の間をつなぐ制御層。"""

    def __init__(self) -> None:
        self._cancel_event = Event()

    def reset_cancel(self) -> None:
        self._cancel_event.clear()

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def _check_cancel(self) -> None:
        if self.is_cancelled():
            raise InterruptedError("ユーザー操作により処理を中断しました。")

    def run(
        self,
        request: ResearchRequest,
        on_progress: ProgressCallback,
        on_log: LogCallback,
    ) -> ResearchResult:
        self.reset_cancel()
        cfg = request.settings
        total_steps = 9

        on_progress("lower_csv", 0, 1, {
            "step": 1, "total_steps": total_steps,
            "message": "下位足CSVを読み込み中",
            "next": "下位足の特徴量生成",
        })
        lower, lower_meta = read_mt5_csv(request.lower_csv)
        self._check_cancel()
        on_log(f"下位足: {lower_meta}")
        on_progress("lower_csv", 1, 1, {
            "step": 1, "total_steps": total_steps,
            "message": f"下位足CSV読込完了（{len(lower):,}本）",
            "next": "下位足の特徴量生成",
        })

        feature_count = {"done": 0}
        estimated_feature_events = max(
            8, len(set(cfg.get("ema_periods", [20, 50, 200]))) + 7
        )

        def lower_feature_progress(message: str) -> None:
            feature_count["done"] += 1
            on_progress("lower_features", feature_count["done"], estimated_feature_events, {
                "step": 2, "total_steps": total_steps,
                "message": message,
                "next": "上位足CSV読込" if request.higher_csv else "探索条件生成",
            })

        features = add_builtin_features(
            lower,
            cfg,
            progress=lower_feature_progress,
            should_cancel=self.is_cancelled,
            label="下位足",
        )
        self._check_cancel()

        if request.higher_csv:
            on_progress("higher_csv", 0, 1, {
                "step": 3, "total_steps": total_steps,
                "message": "上位足CSVを読み込み中",
                "next": "上位足の特徴量生成",
            })
            higher, higher_meta = read_mt5_csv(request.higher_csv)
            self._check_cancel()
            on_log(f"上位足: {higher_meta}")
            on_progress("higher_csv", 1, 1, {
                "step": 3, "total_steps": total_steps,
                "message": f"上位足CSV読込完了（{len(higher):,}本）",
                "next": "上位足の特徴量生成",
            })

            higher_count = {"done": 0}

            def higher_feature_progress(message: str) -> None:
                higher_count["done"] += 1
                on_progress("higher_features", higher_count["done"], estimated_feature_events, {
                    "step": 4, "total_steps": total_steps,
                    "message": message,
                    "next": "上位足・下位足の同期",
                })

            higher_features = add_builtin_features(
                higher,
                cfg,
                progress=higher_feature_progress,
                should_cancel=self.is_cancelled,
                label="上位足",
            )
            self._check_cancel()

            on_progress("align", 0, 1, {
                "step": 5, "total_steps": total_steps,
                "message": "上位足と下位足を同期中",
                "next": "探索条件生成",
            })
            features = align_higher(features, higher_features)
            self._check_cancel()
            on_progress("align", 1, 1, {
                "step": 5, "total_steps": total_steps,
                "message": f"時間足同期完了（{len(features):,}本）",
                "next": "探索条件生成",
            })

        def stage_progress(
            phase: str, done: int, total: int, message: str
        ) -> None:
            if phase == "conditions":
                step = 6
                next_message = "SL/TP結果の事前計算"
            else:
                step = 7
                next_message = "候補条件の自動探索"
            on_progress(phase, done, total, {
                "step": step,
                "total_steps": total_steps,
                "message": message,
                "next": next_message,
            })

        def search_progress(
            done: int,
            total: int,
            detail: dict[str, Any] | None = None,
        ) -> None:
            # core.pyの3引数コールバックに統一。
            # detailがない旧coreでも壊れないよう既定値を持たせる。
            detail = detail or {}
            conditions = detail.get("conditions", "")
            side = detail.get("side", "")
            rr = detail.get("rr", "")
            stop_atr = detail.get("stop_atr", "")
            parts = [str(x) for x in (conditions, side) if x not in ("", None)]
            if rr not in ("", None):
                parts.append(f"RR {rr}")
            if stop_atr not in ("", None):
                parts.append(f"SL ATR×{stop_atr}")
            message = " / ".join(parts) or "候補条件を評価中"
            on_progress("search", done, total, {
                "step": 8,
                "total_steps": total_steps,
                "message": message,
                "next": "ランキング作成・結果保存",
                **detail,
            })

        ranking, _ = auto_discover(
            features,
            cfg,
            progress=search_progress,
            should_cancel=self.is_cancelled,
            stage_progress=stage_progress,
        )
        self._check_cancel()

        on_progress("save", 0, 1, {
            "step": 9, "total_steps": total_steps,
            "message": "ランキング作成・結果を保存中",
            "next": "完了",
        })
        save_outputs(request.output_dir, ranking, features, cfg)
        on_progress("save", 1, 1, {
            "step": 9, "total_steps": total_steps,
            "message": f"保存完了（候補 {len(ranking):,}件）",
            "next": "strategy_ranking.csvを確認",
        })

        return ResearchResult(
            candidate_count=len(ranking),
            output_dir=str(Path(request.output_dir)),
        )
