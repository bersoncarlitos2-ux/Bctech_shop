"""Ligação à API do fornecedor (Marketplace Reseller API).
PROVIDER_MODE=mock -> catálogo de teste, sem gastar saldo.
PROVIDER_MODE=real -> usa o catálogo e as compras reais do fornecedor.
"""
import json
import os
import re
import time

import requests

MODE = os.getenv("PROVIDER_MODE", "mock")
BASE = os.getenv("PROVIDER_BASE_URL", "https://api.bunaistore.shop/v1")
KEY = os.getenv("PROVIDER_API_KEY", "")

_cache = {"t": 0, "data": []}


class ProviderError(Exception):
    """code: auth, no_funds, suspended, notfound, stock, rate, failed, maint, parse, net, http.
    uncertain=True significa: não sabemos se o fornecedor cobrou (ex.: timeout numa compra)."""

    def __init__(self, msg, code="", uncertain=False):
        super().__init__(msg)
        self.code = code
        self.uncertain = uncertain

    @property
    def internal(self):
        """Erros que o cliente não deve ver (são problemas teus com o fornecedor)."""
        return self.code in ("auth", "no_funds", "suspended")

    @property
    def public(self):
        if self.internal:
            return "Produto indisponível de momento. Tenta mais tarde."
        return str(self)


_ERRORS = {
    401: ("auth", "Chave do fornecedor inválida."),
    402: ("no_funds", "Sem saldo no fornecedor."),
    403: ("suspended", "Conta do fornecedor suspensa."),
    404: ("notfound", "Produto não encontrado."),
    409: ("stock", "Stock esgotado, tenta outro."),
    429: ("rate", "Muitos pedidos, tenta de novo em 1 minuto."),
    502: ("failed", "O fornecedor falhou a compra (não foi cobrado)."),
    503: ("maint", "Fornecedor em manutenção."),
}


def _req(method, path, **kw):
    if MODE != "mock" and not KEY:
        raise ProviderError("Chave do fornecedor não configurada.", code="auth")
    is_post = method.upper() == "POST"
    try:
        r = requests.request(method, BASE + path, headers={"X-API-Key": KEY}, timeout=30, **kw)
    except requests.RequestException:
        raise ProviderError("Fornecedor indisponível, tenta daqui a pouco.", code="net", uncertain=is_post)
    if r.status_code in _ERRORS:
        code, msg = _ERRORS[r.status_code]
        raise ProviderError(msg, code=code)
    if r.status_code >= 400:
        # erro 5xx numa compra: não sabemos se cobrou
        raise ProviderError(f"Erro do fornecedor ({r.status_code}).", code="http",
                            uncertain=is_post and r.status_code >= 500)
    try:
        return r.json()
    except ValueError:
        raise ProviderError("Resposta inválida do fornecedor.", code="parse", uncertain=is_post)


def _clean(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _first(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _as_list(data):
    """Aceita lista direta ou {'products': [...]} / {'items': [...]} / {'data': [...]}."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("products", "items", "data", "results"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


# ---------- teste ----------
_MOCK = [
    {"id": "m1", "group_id": "gn", "group_name": "Netflix", "variant": "Premium 1 mês",
     "name": "Netflix > Premium 1 mês", "cost": 3.5, "stock": 12, "infinite": False},
    {"id": "m2", "group_id": "gn", "group_name": "Netflix", "variant": "Premium 3 meses",
     "name": "Netflix > Premium 3 meses", "cost": 8.0, "stock": 5, "infinite": False},
    {"id": "m3", "group_id": "ga", "group_name": "Amazon Prime", "variant": "1 mês",
     "name": "Amazon Prime > 1 mês", "cost": 1.0, "stock": 9999, "infinite": True},
]


def catalog():
    """Lista de produtos com entrega automática."""
    if MODE == "mock":
        return _MOCK
    if _cache["data"] and time.time() - _cache["t"] < 60:
        return _cache["data"]
    out, offset = [], 0
    for _ in range(10):  # máximo 1000 produtos
        data = _req("GET", "/products",
                    params={"view": "variants", "type": "auto", "limit": 100, "offset": offset})
        page = _as_list(data)
        for p in page:
            try:
                pid = str(_first(p, "product_id", "id"))
                if pid in ("None", ""):
                    continue
                infinite = bool(_first(p, "infinite_stock", "infinite", default=False))
                stock = _int(_first(p, "stock_count", "stock", "available"))
                if not infinite and stock is not None and stock <= 0:
                    continue  # esgotado: não mostrar
                out.append({
                    "id": pid,
                    "group_id": _first(p, "group_id"),
                    "group_name": _first(p, "group_name"),
                    "variant": _first(p, "variant_name", "variant"),
                    "name": _first(p, "display_name", "name", "variant_name", default=pid),
                    "cost": float(p["price"]),
                    "stock": stock,
                    "infinite": infinite,
                })
            except (KeyError, TypeError, ValueError):
                continue  # produto com formato estranho: ignora
        if len(page) < 100:
            break
        offset += 100
    _cache.update(t=time.time(), data=out)
    return out


def product(pid):
    """Detalhes (descrição, garantia, stock) antes de comprar."""
    if MODE == "mock":
        p = next((x for x in _MOCK if x["id"] == pid), None)
        if not p:
            raise ProviderError("Produto não encontrado.", code="notfound")
        return {**p, "note": "Conta de demonstração. Não usar em produção.", "warranty_hours": 24}
    d = _req("GET", f"/products/{pid}")
    if isinstance(d, dict) and isinstance(d.get("product"), dict):
        d = d["product"]
    try:
        return {
            "id": str(_first(d, "product_id", "id", default=pid)),
            "name": _first(d, "display_name", "name", default=pid),
            "note": _clean(_first(d, "note", "description", default="")),
            "cost": float(d["price"]),
            "stock": _int(_first(d, "stock_count", "stock", "available")),
            "infinite": bool(_first(d, "infinite_stock", "infinite", default=False)),
            "warranty_hours": _int(_first(d, "warranty_hours", default=0)) or 0,
        }
    except (KeyError, TypeError, ValueError):
        raise ProviderError("Resposta inesperada do fornecedor.", code="parse")


def buy(pid):
    """Compra 1 unidade. Devolve {'ref', 'items', 'after_note'}."""
    if MODE == "mock":
        return {"ref": f"mock-{int(time.time())}", "items": ["demo@email.com:senha123"],
                "after_note": "Usa uma VPN do Canadá para entrar."}
    data = _req("POST", "/orders", json={"product_id": pid, "qty": 1, "include_after_note": True})
    try:
        o = data.get("order", data)
        items = []
        for it in (o.get("items") or []):
            items.append(it if isinstance(it, str) else json.dumps(it, ensure_ascii=False))
        ref = str(_first(o, "order_id", "id", default=""))
        return {"ref": ref, "items": items, "after_note": _clean(o.get("after_note"))}
    except (AttributeError, TypeError):
        # a compra foi feita mas não conseguimos ler a resposta
        raise ProviderError("Resposta inesperada do fornecedor.", code="parse", uncertain=True)


def get_balance():
    if MODE == "mock":
        return 100.0
    d = _req("GET", "/me")
    for src in (d, d.get("wallet") if isinstance(d, dict) else None):
        v = _first(src, "balance", "wallet_balance", "credit")
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    raise ProviderError("Não encontrei o saldo na resposta. Usa /teste.", code="parse")


def diagnostico():
    """Mostra a forma das respostas reais da API (sem mostrar a chave)."""
    if MODE == "mock":
        return "Estás em modo MOCK. Muda PROVIDER_MODE para real para testar a API."
    lines = []
    try:
        d = _req("GET", "/me")
        keys = ", ".join(sorted(d.keys())) if isinstance(d, dict) else type(d).__name__
        lines.append(f"/me campos: {keys}")
    except ProviderError as e:
        lines.append(f"/me erro: {e}")
    try:
        d = _req("GET", "/products", params={"view": "variants", "type": "auto", "limit": 1})
        lines.append(f"/products tipo: {type(d).__name__}")
        page = _as_list(d)
        if page:
            lines.append("1.º produto: " + json.dumps(page[0], ensure_ascii=False)[:1200])
        else:
            lines.append("/products sem produtos reconhecidos: " + json.dumps(d, ensure_ascii=False)[:600])
    except ProviderError as e:
        lines.append(f"/products erro: {e}")
    try:
        lines.append(f"Saldo lido: ${get_balance():.2f}")
    except ProviderError as e:
        lines.append(f"Saldo erro: {e}")
    return "\n".join(lines)
