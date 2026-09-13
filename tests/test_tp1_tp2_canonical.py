"""Release-gate tests for BARON's canonical 50/50 profit plan."""
import importlib, os, sys, types, unittest

class _FakeFlask:
    def route(self,*a,**k): return lambda fn: fn
    def add_url_rule(self,*a,**k): return None
    def __init__(self,*a,**k): pass

def load():
    saved={k:sys.modules.get(k) for k in ('ccxt','flask')}
    ccxt=types.ModuleType('ccxt')
    class B:
        def __init__(self,*a,**k): self.markets={'BTC/USDT:USDT':{}}
    ccxt.bingx=B
    flask=types.ModuleType('flask'); flask.Flask=_FakeFlask; flask.jsonify=lambda *a,**k:None; flask.request=types.SimpleNamespace()
    sys.modules['ccxt']=ccxt; sys.modules['flask']=flask; sys.modules.pop('core.engine',None)
    e=importlib.import_module('core.engine')
    return e,saved

class CanonicalTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.e,cls.saved=load()
    @classmethod
    def tearDownClass(cls):
        sys.modules.pop('core.engine',None)
        for k,v in cls.saved.items():
            if v is None: sys.modules.pop(k,None)
            else: sys.modules[k]=v
    def setUp(self):
        e=self.e; e.PAPER_MODE=True
        e.STATE.update({'open':True,'side':'BUY','entry':100.0,'qty':100.0,'qty_initial':100.0,'remaining_qty':100.0,
                        'margin':10.0,'current_symbol':'BTC/USDT','mark_price':100.0,'tp1_price':110.0,'tp2_price':120.0,
                        'tp1_hit':False,'tp2_hit':False,'tp1_closed_qty':0.0,'partial_realized':[]})
        e.paper={'balance':1000.0,'position':{'side':'BUY','entry':100.0,'qty':100.0,'remaining_qty':100.0},'committed_margin':10.0}
    def test_tp1_is_exactly_half_original_and_idempotent(self):
        e=self.e; e.STATE['mark_price']=110.0
        self.assertTrue(e.close_partial(0.5, stage='TP1'))
        self.assertEqual(e.STATE['remaining_qty'],50.0)
        self.assertEqual(e.STATE['tp1_closed_qty'],50.0)
        self.assertTrue(e.close_partial(0.5, stage='TP1'))
        self.assertEqual(e.STATE['remaining_qty'],50.0)
        self.assertEqual(e.STATE['tp1_closed_qty'],50.0)
    def test_generic_partial_is_not_tp1_stage(self):
        e=self.e; e.STATE['mark_price']=105.0
        self.assertTrue(e.close_partial(0.5))
        self.assertEqual(e.STATE['remaining_qty'],50.0)
        self.assertEqual(e.STATE.get('tp1_closed_qty',0.0),0.0)
    def test_canonical_geometry(self):
        e=self.e
        self.assertLess(90.0,100.0); self.assertLess(100.0,e.STATE['tp1_price']); self.assertLess(e.STATE['tp1_price'],e.STATE['tp2_price'])
        e.STATE.update({'side':'SELL','entry':100.0,'tp1_price':90.0,'tp2_price':80.0})
        self.assertLess(e.STATE['tp2_price'],e.STATE['tp1_price']); self.assertLess(e.STATE['tp1_price'],e.STATE['entry'])
