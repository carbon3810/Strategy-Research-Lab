
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Dict, Any
import pandas as pd

IndicatorFunc = Callable[[pd.DataFrame, Dict[str, Any]], pd.Series | pd.DataFrame]
SignalFunc = Callable[[pd.DataFrame, Dict[str, Any]], pd.Series]

@dataclass
class Registry:
    indicators: dict[str, IndicatorFunc]
    signals: dict[str, SignalFunc]

    def __init__(self):
        self.indicators = {}
        self.signals = {}

    def indicator(self, name: str):
        def deco(func: IndicatorFunc):
            self.indicators[name] = func
            return func
        return deco

    def signal(self, name: str):
        def deco(func: SignalFunc):
            self.signals[name] = func
            return func
        return deco

registry = Registry()
