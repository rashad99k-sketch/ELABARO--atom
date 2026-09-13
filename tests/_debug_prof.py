from tests.test_profit_engine_phase3 import ProfessionalTradingScenarioTest

def test_dbg():
    t=ProfessionalTradingScenarioTest(methodName='test_ifvg_warning_survives_six_position_manage_cycle'); t.setUp()
    base={s:t.pm.contexts[s].state['entry'] for s in t.pm.symbols()}
    for s,m in {"BTC/USDT:USDT":1.004,"US500/USDT:USDT":1.004,"XAUUSD":1.004,"ETH/USDT:USDT":0.996,"USTECH/USDT:USDT":0.996,"WTI":0.996}.items(): t.live[s]=base[s]*m
    t._advance_clock(); t.pm.manage_all(); print('AFTER',t.pm.symbols());
    for m in t.logs:
      if any(x in m for x in ('[HARD_EXIT]','[SYNTHETIC_SL]','[TRAIL]','[SYNTHETIC_TP2]','[PPE]','[THESIS_FAILURE]','[PROFIT_LOCK]','[CLOSE]','TP1 hit')): print(m)
