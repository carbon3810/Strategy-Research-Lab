
import pandas as pd
from plugin_api import registry

@registry.indicator("rsi")
def rsi(df: pd.DataFrame, params: dict) -> pd.Series:
    n=int(params.get("period",14))
    d=df.close.diff()
    up=d.clip(lower=0).ewm(alpha=1/n,adjust=False).mean()
    down=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean()
    rs=up/down.replace(0,float("nan"))
    return 100-(100/(1+rs))
