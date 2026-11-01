#!/usr/bin/env python3
"""
Shadow Spread Market Making V1.5
Pro Asset im Depot:
- max. 1 BUY + 1 SELL
- eine Seite 100 % gefüllt → Gegenseite cancel, beide neu zum Last
- beide Seiten älter als 1 h (Stale) → beide cancel, beide neu
- BUY/SELL: Fraction, mind. 1 Stück (auch wenn Kurs > 1 Mio €),
  sonst max. 1.000.000 € Volumen
- kein hourlyChange / kein Decay
- Spread = MIN_SPREAD symmetrisch um Last
- Cent-Preise; ±20 % vom Last, mind. ±0,01 €
- SELL nie unter averageBuyingPrice
"""

import time
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import requests

# =========================
# KONFIGURATION
# =========================
BASE_URL = "https://stable.alpha-trader.com"
JWT = (
    "insert JWT here"
)
OWNER_ID = "inser Owneer ID here"

MIN_SPREAD = 0.05
MAX_DISTANCE = 0.20
MIN_TICK = Decimal("0.01")
CASH_FRACTION = 0.01
SHARES_FRACTION = 0.01
MAX_ORDER_VOLUME = 100_000_000.0   # max. Euro pro Order, außer bei 1 Stück
STALE_SECONDS = 3600
LOOP_SECONDS = 60

HEADERS = {
    "accept": "*/*",
    "Authorization": f"Bearer {JWT}",
}


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(MIN_TICK, rounding=ROUND_HALF_UP)


def order_age_seconds(order: dict) -> float:
    created = order.get("creationDate")
    if created is None:
        return 0.0
    created = float(created)
    if created > 10_000_000_000:
        created = created / 1000.0
    return max(0.0, time.time() - created)


def get_portfolio() -> dict:
    r = requests.get(
        f"{BASE_URL}/api/portfolios/{OWNER_ID}",
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def get_open_orders() -> list:
    r = requests.get(
        f"{BASE_URL}/api/securityorders/securitiesaccount/{OWNER_ID}",
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def get_price_spread(security_id: str) -> dict:
    r = requests.get(
        f"{BASE_URL}/api/pricespreads/{security_id}",
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def place_order(
    security_id: str,
    action: str,
    price: Decimal,
    shares: int,
) -> Optional[dict]:
    if shares < 1:
        return None
    params = {
        "owner": OWNER_ID,
        "securityIdentifier": security_id,
        "action": action,
        "type": "LIMIT",
        "price": f"{price:.2f}",
        "numberOfShares": int(shares),
    }
    r = requests.post(
        f"{BASE_URL}/api/securityorders",
        headers=HEADERS,
        params=params,
        data="",
        timeout=30,
    )
    if r.status_code >= 400:
        print(
            f"  [{ts()}] Order-Fehler {action} {security_id} @ {price}: "
            f"{r.status_code} {r.text[:220]}"
        )
        return None
    print(f"  [{ts()}] {action} {int(shares)} @ {price}")
    return r.json() if r.text else {"ok": True}


def cancel_order(order_id: str) -> None:
    r = requests.delete(
        f"{BASE_URL}/api/securityorders/{order_id}",
        headers=HEADERS,
        timeout=30,
    )
    if r.status_code >= 400:
        print(f"  [{ts()}] Cancel-Fehler {order_id}: {r.status_code} {r.text[:120]}")
    else:
        print(f"  [{ts()}] cancel {order_id[:8]}…")


def positions_from_portfolio(portfolio: dict) -> list:
    out = []
    for pos in portfolio.get("positions") or portfolio.get("securities") or []:
        sec = pos.get("security") or pos
        sid = (
            sec.get("securityIdentifier")
            or pos.get("securityIdentifier")
            or pos.get("listing", {}).get("securityIdentifier")
        )
        if not sid:
            continue
        name = (
            (sec.get("name") if isinstance(sec, dict) else None)
            or pos.get("name")
            or sid
        )
        qty = float(pos.get("numberOfShares") or pos.get("shares") or 0)
        committed = float(pos.get("committedShares") or 0)
        free = max(0.0, qty - committed)
        avg = float(
            pos.get("averageBuyingPrice")
            or pos.get("averagePrice")
            or pos.get("avgPrice")
            or 0
        )
        if free <= 0 and qty <= 0:
            continue
        out.append(
            {
                "security_id": sid,
                "name": name,
                "avg_buy": avg,
                "free_shares": free,
            }
        )
    return out


def clamp_band(last: Decimal):
    dist = max(last * Decimal(str(MAX_DISTANCE)), MIN_TICK)
    low = money(max(last - dist, MIN_TICK))
    high = money(last + dist)
    return low, high


def compute_prices(last: Decimal, avg_buy: float):
    last = money(last) if last >= MIN_TICK else MIN_TICK

    half = Decimal(str(MIN_SPREAD)) / Decimal("2")
    bid = money(last * (Decimal("1") - half))
    ask = money(last * (Decimal("1") + half))

    if ask <= bid:
        ask = money(bid + MIN_TICK)

    low, high = clamp_band(last)
    bid = max(bid, low)
    ask = min(ask, high)
    if ask <= bid:
        ask = money(bid + MIN_TICK)

    floor = money(avg_buy) if avg_buy and avg_buy > 0 else MIN_TICK
    if ask < floor:
        ask = floor
        target_bid = money(ask / (Decimal("1") + Decimal(str(MIN_SPREAD))))
        bid = min(bid, target_bid)
        if bid >= ask:
            bid = money(ask - MIN_TICK)
        if bid < MIN_TICK:
            bid = MIN_TICK

    return money(bid), money(ask)


def place_both(
    security_id: str,
    last: Decimal,
    avg_buy: float,
    free_shares: float,
    cash: float,
) -> None:
    bid_px, ask_px = compute_prices(last, avg_buy)

    # --- BUY: Fraction, max. 1 Mio €, aber immer mind. 1 Stück wenn Cash reicht ---
    buy_shares = 0
    if cash > 0 and bid_px > 0:
        budget = min(cash * CASH_FRACTION, MAX_ORDER_VOLUME)
        buy_shares = int(budget / float(bid_px))
        if buy_shares < 1 and cash >= float(bid_px):
            buy_shares = 1  # auch wenn 1 Stück > 1 Mio €
        elif buy_shares >= 1 and buy_shares * float(bid_px) > MAX_ORDER_VOLUME:
            buy_shares = max(1, int(MAX_ORDER_VOLUME / float(bid_px)))
            if buy_shares < 1 and cash >= float(bid_px):
                buy_shares = 1

    # --- SELL: Fraction, max. 1 Mio €, aber immer mind. 1 Stück wenn frei ---
    sell_shares = int(free_shares * SHARES_FRACTION)
    if sell_shares < 1 and free_shares >= 1:
        sell_shares = 1
    if sell_shares >= 1 and ask_px > 0:
        if sell_shares * float(ask_px) > MAX_ORDER_VOLUME:
            capped = int(MAX_ORDER_VOLUME / float(ask_px))
            sell_shares = capped if capped >= 1 else 1
        if sell_shares < 1 and free_shares >= 1:
            sell_shares = 1

    if buy_shares >= 1:
        place_order(security_id, "BUY", bid_px, buy_shares)
    else:
        print("  kein BUY (Cash/Größe)")

    if sell_shares >= 1:
        place_order(security_id, "SELL", ask_px, sell_shares)
    else:
        print("  kein SELL (Stücke/Größe)")


def fetch_last(security_id: str) -> Optional[Decimal]:
    try:
        ps = get_price_spread(security_id)
    except Exception as e:
        print(f"  Preisabruf fehlgeschlagen: {e}")
        return None
    last_raw = (
        (ps.get("lastPrice") or {}).get("value")
        or ps.get("bidPrice")
        or ps.get("askPrice")
    )
    if last_raw is None:
        print("  kein Last-Preis – skip")
        return None
    return money(last_raw)


def run_for_security(
    security_id: str,
    name: str,
    avg_buy: float,
    free_shares: float,
    cash: float,
    all_orders: list,
) -> None:
    own = [o for o in all_orders if o.get("securityIdentifier") == security_id]
    buys = [o for o in own if o.get("action") == "BUY"]
    sells = [o for o in own if o.get("action") == "SELL"]

    print(f"\n[{ts()}] {name} ({security_id})")
    print(f"  BUY: {len(buys)} | SELL: {len(sells)} | frei: {free_shares:,.0f}")

    if buys and sells:
        ages = [order_age_seconds(o) for o in buys + sells]
        max_age = max(ages) if ages else 0.0
        if max_age < STALE_SECONDS:
            print(f"  Quotes stehen – ok (max. Alter {max_age/60:.1f} min)")
            return
        print(f"  Stale ({max_age/60:.1f} min ≥ {STALE_SECONDS/60:.0f} min) → beide neu")
        for o in buys + sells:
            oid = o.get("id")
            if oid:
                cancel_order(oid)
        time.sleep(0.2)
        last = fetch_last(security_id)
        if last is None:
            return
        place_both(security_id, last, avg_buy, free_shares, cash)
        return

    last = fetch_last(security_id)
    if last is None:
        return

    if buys and not sells:
        print("  SELL gefüllt → BUY cancel, beide neu")
        for o in buys:
            oid = o.get("id")
            if oid:
                cancel_order(oid)
        time.sleep(0.2)
        place_both(security_id, last, avg_buy, free_shares, cash)
        return

    if sells and not buys:
        print("  BUY gefüllt → SELL cancel, beide neu")
        for o in sells:
            oid = o.get("id")
            if oid:
                cancel_order(oid)
        time.sleep(0.2)
        place_both(security_id, last, avg_buy, free_shares, cash)
        return

    print("  keine Quotes → beide neu")
    place_both(security_id, last, avg_buy, free_shares, cash)


def run_once() -> None:
    print(f"\n[{ts()}] === Zyklus ===")
    portfolio = get_portfolio()
    cash = float(portfolio.get("cash") or 0)
    positions = positions_from_portfolio(portfolio)
    orders = get_open_orders()

    print(f"Cash: {cash:,.2f} | Positionen: {len(positions)} | Orders: {len(orders)}")

    for pos in positions:
        try:
            run_for_security(
                security_id=pos["security_id"],
                name=pos["name"],
                avg_buy=pos["avg_buy"],
                free_shares=pos["free_shares"],
                cash=cash,
                all_orders=orders,
            )
        except Exception as e:
            print(f"  Fehler {pos['security_id']}: {e}")
        time.sleep(0.25)


def main() -> None:
    print(
        "Shadow Spread Market Making V1.5\n"
        "Fill → Gegenseite cancel + beide neu | "
        f"Stale nach {STALE_SECONDS // 60} min | "
        f"min. 1 Stück (auch >1 Mio €) | max. {MAX_ORDER_VOLUME:,.0f} € sonst | "
        f"Spread={MIN_SPREAD:.0%} | BUY={CASH_FRACTION:.1%} Cash\n"
        f"Owner={OWNER_ID}\n"
    )
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[{ts()}] Zyklus-Fehler: {e}")
        print(f"\n[{ts()}] Warte {LOOP_SECONDS}s …")
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()