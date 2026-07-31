
from plugin_api import registry

@registry.signal("下ヒゲピンバー")
def lower_pin(df, params):
    wick=float(params.get("min_wick_ratio",0.55))
    body=float(params.get("max_body_ratio",0.35))
    return (df.lower_wick_ratio>=wick) & (df.body_ratio<=body)

@registry.signal("上ヒゲピンバー")
def upper_pin(df, params):
    wick=float(params.get("min_wick_ratio",0.55))
    body=float(params.get("max_body_ratio",0.35))
    return (df.upper_wick_ratio>=wick) & (df.body_ratio<=body)
