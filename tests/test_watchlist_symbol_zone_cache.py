import numpy as np
import pandas as pd
import core.engine as E


def _df(n=40):
    x = np.linspace(100.0, 104.0, n)
    return pd.DataFrame({
        'open': x - 0.2, 'high': x + 0.4, 'low': x - 0.4,
        'close': x, 'volume': np.full(n, 1000.0)
    })


def test_entry_condition_uses_real_symbol_for_zone_lookup(monkeypatch):
    seen = []
    monkeypatch.setattr(E, 'compute_adx', lambda df: pd.Series([30.0] * len(df), index=df.index))
    monkeypatch.setattr(E, 'classify_volume', lambda df: 'normal')
    def zones(symbol, df, *args, **kwargs):
        seen.append(symbol)
        return {'buy_zones': [], 'sell_zones': []}
    monkeypatch.setattr(E, 'get_smart_zones', zones)
    radar = E.ExecutionQueue()
    assert radar._check_entry_conditions(_df(), 'BUY', 1.0, 'SYMBOL-A')
    assert seen == ['SYMBOL-A']
