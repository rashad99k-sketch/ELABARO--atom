"""Exchange-native conditional protection adapter.

The adapter is fail-closed: it only reports PROTECTED after the exchange
returns an order id.  Because conditional-order parameters differ by venue,
activation is explicit via ENABLE_NATIVE_PROTECTION=1 and the order type/extra
params are configurable. Synthetic management remains the fallback.
"""
from __future__ import annotations
import json, os, time

class NativeProtectionManager:
    def __init__(self, exchange=None, logger=None):
        self.exchange=exchange
        self.logger=logger or (lambda *a,**k:None)
        self.enabled=os.getenv("ENABLE_NATIVE_PROTECTION","0").lower() in {"1","true","yes","on"}
        self.order_type=os.getenv("NATIVE_PROTECTION_ORDER_TYPE","STOP_MARKET")
        self._orders={}
    def status(self,symbol):
        o=self._orders.get(symbol)
        if o and o.get("sl_order_id"): return "PROTECTED"
        return "UNPROTECTED"
    def _params(self, side, position_side, trigger):
        params={"positionSide":position_side,"triggerPrice":float(trigger)}
        raw=os.getenv("NATIVE_PROTECTION_PARAMS_JSON","").strip()
        if raw:
            try: params.update(json.loads(raw))
            except Exception: pass
        return params
    def place(self,symbol,side,qty,sl,position_side):
        if not self.enabled:return {"status":"DISABLED"}
        if self.exchange is None or not sl or qty<=0:return {"status":"UNPROTECTED","reason":"MISSING_EXCHANGE_OR_LEVEL"}
        close_side="sell" if str(side).upper()=="BUY" else "buy"
        try:
            order=self.exchange.create_order(symbol,self.order_type,close_side,float(qty),None,self._params(side,position_side,sl))
            oid=(order or {}).get("id")
            if not oid: return {"status":"UNPROTECTED","reason":"NO_ORDER_ID"}
            # A create-order ACK is not sufficient for live protection: some
            # venues/adapters can acknowledge a conditional request before its
            # final order state is visible.  When fetch_order is available,
            # re-read the exact order and fail closed on rejected/cancelled
            # states.
            verify = os.getenv("NATIVE_PROTECTION_VERIFY", "1").strip().lower() in {"1","true","yes","on"}
            if verify:
                fetch_order = getattr(self.exchange, "fetch_order", None)
                if not callable(fetch_order):
                    return {"status":"UNPROTECTED","reason":"NO_ORDER_VERIFIER"}
                try:
                    confirmed = fetch_order(oid, symbol)
                    if not isinstance(confirmed, dict):
                        return {"status":"UNPROTECTED","reason":"INVALID_ORDER_VERIFICATION"}
                    confirmed_id = confirmed.get("id") or (confirmed.get("info") or {}).get("orderId")
                    status = str(confirmed.get("status") or "").lower()
                    if confirmed_id and str(confirmed_id) != str(oid):
                        return {"status":"UNPROTECTED","reason":"ORDER_ID_MISMATCH"}
                    if status in {"canceled","cancelled","rejected","expired","failed"}:
                        return {"status":"UNPROTECTED","reason":f"ORDER_STATUS_{status.upper()}"}
                except Exception as verify_exc:
                    self.logger(f"[NATIVE_PROTECTION] {symbol} verification failed: {verify_exc}","ERROR")
                    return {"status":"UNPROTECTED","reason":"ORDER_VERIFICATION_FAILED"}
            self._orders[symbol]={"sl_order_id":oid,"sl":float(sl),"updated_at":time.time()}
            self.logger(f"[NATIVE_PROTECTION] {symbol} SL ACK id={oid} level={sl}","SUCCESS")
            return {"status":"PROTECTED","sl_order_id":oid,"sl":float(sl)}
        except Exception as exc:
            self.logger(f"[NATIVE_PROTECTION] {symbol} placement failed: {exc}","ERROR")
            return {"status":"UNPROTECTED","reason":str(exc)}
    def update(self,symbol,side,qty,sl,position_side):
        if not self.enabled:return {"status":"DISABLED"}
        old=(self._orders.get(symbol) or {}).get("sl_order_id")
        result=self.place(symbol,side,qty,sl,position_side)
        new=result.get("sl_order_id")
        # Place-first, cancel-second prevents a protection gap if the new order
        # is rejected. The old stop remains active in that case.
        if result.get("status")=="PROTECTED" and old and old != new:
            try:self.exchange.cancel_order(old,symbol)
            except Exception as exc:self.logger(f"[NATIVE_PROTECTION] old SL cancel failed: {exc}","WARN")
        return result

    def cancel(self,symbol):
        item=self._orders.pop(symbol,None)
        if not item or not self.enabled:return True
        oid=item.get("sl_order_id")
        if not oid:return True
        try:
            self.exchange.cancel_order(oid,symbol)
            self.logger(f"[NATIVE_PROTECTION] {symbol} SL cancelled id={oid}","INFO")
            return True
        except Exception as exc:
            self.logger(f"[NATIVE_PROTECTION] {symbol} cancel failed: {exc}","WARN")
            return False
