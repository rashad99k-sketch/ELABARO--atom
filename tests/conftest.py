"""Offline test boundary.

The production project depends on ccxt/Flask at runtime. The execution sandbox
used for this validation pass intentionally has no network package installer,
so tests that exercise the real engine receive a dependency-only ccxt stub.
Individual dashboard tests that need Flask already provide their own boundary
stub. No exchange method in this file can place a real order.
"""
import sys
import types

if "ccxt" not in sys.modules:
    ccxt = types.ModuleType("ccxt")
    class FakeExchange:
        def __init__(self, *args, **kwargs):
            self.markets = {}
            self.has = {}
            self.options = kwargs.get("options", {}) if kwargs else {}
        def load_markets(self, *args, **kwargs): return self.markets
        def fetch_ticker(self, *args, **kwargs): return {}
        def fetch_tickers(self, *args, **kwargs): return {}
        def fetch_ohlcv(self, *args, **kwargs): return []
        def fetch_order_book(self, *args, **kwargs): return {"bids": [], "asks": []}
        def fetch_positions(self, *args, **kwargs): return []
        def fetch_my_trades(self, *args, **kwargs): return []
        def create_order(self, *args, **kwargs): raise RuntimeError("TEST_BOUNDARY: live order blocked")
        def cancel_order(self, *args, **kwargs): return {"id": args[0] if args else "test"}
        def amount_to_precision(self, symbol, amount): return str(amount)
        def price_to_precision(self, symbol, price): return str(price)
        def market(self, symbol): return self.markets.get(symbol, {"limits":{"amount":{"min":0}},"precision":{"amount":1}})
        def set_leverage(self, *args, **kwargs): return None
    ccxt.bingx = FakeExchange
    sys.modules["ccxt"] = ccxt

# Minimal Flask compatibility layer for this offline validation environment.
if "flask" not in sys.modules:
    flask = types.ModuleType("flask")
    import json as _json
    class _Request:
        remote_addr = "127.0.0.1"
        headers = {}
        json = {}
        def get_json(self, silent=False): return dict(self.json or {})
    request = _Request()
    class _Response:
        def __init__(self, value, status=200): self._value=value; self.status_code=status
        def get_json(self): return self._value
        def get_data(self, as_text=False):
            data=_json.dumps(self._value)
            return data if as_text else data.encode()
    def jsonify(*args, **kwargs):
        if kwargs: value=kwargs
        elif len(args)==1: value=args[0]
        else: value=list(args)
        return _Response(value,200)
    class Flask:
        def __init__(self, name, *args, **kwargs): self.name=name; self.url_map=types.SimpleNamespace(); self._routes={}; self._before=[]
        def route(self, rule, **options):
            def deco(fn): self._routes[(rule, tuple(sorted(options.get("methods", ["GET"]))))]=fn; return fn
            return deco
        def add_url_rule(self, rule, endpoint=None, view_func=None, methods=None, **kwargs):
            self._routes[(rule, tuple(sorted(methods or ["GET"])))]=view_func
        def before_request(self, fn): self._before.append(fn); return fn
        def run(self, *a, **k): return None
        def test_client(self):
            app=self
            class Client:
                def _call(self, method, path, json=None, headers=None):
                    request.remote_addr="127.0.0.1"; request.headers=headers or {}; request.json=json or {}
                    for fn in app._before: fn()
                    fn=None
                    for (rule, methods), candidate in app._routes.items():
                        if rule==path and method in methods: fn=candidate; break
                    if fn is None: return _Response({"error":"not found"},404)
                    out=fn()
                    if isinstance(out, tuple): return _Response(out[0]._value if isinstance(out[0],_Response) else out[0], out[1])
                    return out if isinstance(out,_Response) else _Response(out,200)
                def get(self,path,**kw): return self._call("GET",path,**kw)
                def post(self,path,**kw): return self._call("POST",path,**kw)
                def __enter__(self): return self
                def __exit__(self,*a): return False
            return Client()
    flask.Flask=Flask; flask.jsonify=jsonify; flask.request=request
    import importlib.machinery as _machinery
    flask.__spec__ = _machinery.ModuleSpec("flask", loader=None)
    sys.modules["flask"]=flask

def pytest_runtest_setup(item):
    # A few legacy tests deliberately replace dependency modules with their own
    # stubs. importlib.util.find_spec requires __spec__ on those stubs.
    import importlib.machinery
    for name in ("ccxt", "flask"):
        mod = sys.modules.get(name)
        if mod is not None and getattr(mod, "__spec__", None) is None:
            mod.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
