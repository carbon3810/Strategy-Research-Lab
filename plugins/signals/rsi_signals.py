
from plugin_api import registry

@registry.signal("RSI_売られすぎ")
def rsi_oversold(df, params):
    level=float(params.get("level",30))
    return df["rsi"] < level

@registry.signal("RSI_買われすぎ")
def rsi_overbought(df, params):
    level=float(params.get("level",70))
    return df["rsi"] > level
