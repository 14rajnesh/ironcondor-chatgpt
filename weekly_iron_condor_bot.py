import os
import json
import math
import requests
import traceback
from datetime import datetime, date, time
from typing import Any, Dict, List, Optional, Tuple

import pytz
from dateutil import parser as date_parser
from fyers_apiv3 import fyersModel
from supabase import create_client

# ============================================================
# WEEKLY NIFTY IRON CONDOR PAPER BOT
# Paper trading only. No real broker orders are sent.
# ============================================================

IST = pytz.timezone("Asia/Kolkata")

# Credentials from GitHub Secrets / environment
FYERS_CLIENT_ID = os.getenv("FYERS_CLIENT_ID")
FYERS_ACCESS_TOKEN = os.getenv("FYERS_ACCESS_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Bot configuration
UNDERLYING = os.getenv("UNDERLYING", "NSE:NIFTY50-INDEX")
VIX_SYMBOL = os.getenv("VIX_SYMBOL", "NSE:INDIAVIX-INDEX")
TABLE_NAME = os.getenv("TABLE_NAME", "paper_iron_condor_trades")

LOT_SIZE = int(os.getenv("LOT_SIZE", "75"))
STRIKE_STEP = int(os.getenv("STRIKE_STEP", "50"))
STRIKECOUNT = int(os.getenv("STRIKECOUNT", "60"))
WING_WIDTH = int(os.getenv("WING_WIDTH", "100"))

ENTRY_DTE_MIN = int(os.getenv("ENTRY_DTE_MIN", "2"))
ENTRY_DTE_MAX = int(os.getenv("ENTRY_DTE_MAX", "6"))

# Liquidity rules
MIN_VOLUME = int(os.getenv("MIN_VOLUME", "1000"))
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "12"))
MAX_ABS_SPREAD = float(os.getenv("MAX_ABS_SPREAD", "25"))

# Strike selection. First tries delta if FYERS provides it. Otherwise uses OTM percent.
SELL_DELTA_MIN = float(os.getenv("SELL_DELTA_MIN", "0.23"))
SELL_DELTA_MAX = float(os.getenv("SELL_DELTA_MAX", "0.30"))
TARGET_DELTA = float(os.getenv("TARGET_DELTA", "0.25"))
OTM_PCT_MIN = float(os.getenv("OTM_PCT_MIN", "0.010"))
OTM_PCT_MAX = float(os.getenv("OTM_PCT_MAX", "0.022"))

# Adjustment rules. No stop-loss exit; only paper adjustments and expiry settlement.
TRIGGER_DISTANCE = float(os.getenv("TRIGGER_DISTANCE", "75"))
RECENTER_MOVE = float(os.getenv("RECENTER_MOVE", "250"))
MAX_TOTAL_ADJUSTMENTS = int(os.getenv("MAX_TOTAL_ADJUSTMENTS", "6"))
MAX_ADJUSTMENTS_PER_DAY = int(os.getenv("MAX_ADJUSTMENTS_PER_DAY", "1"))
SETTLE_AFTER_HOUR = int(os.getenv("SETTLE_AFTER_HOUR", "15"))
SETTLE_AFTER_MINUTE = int(os.getenv("SETTLE_AFTER_MINUTE", "20"))


# ============================================================
# BASIC HELPERS
# ============================================================

def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> date:
    return now_ist().date()


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            value = value.replace(",", "")
        return float(value)
    except Exception:
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            value = value.replace(",", "")
        return int(float(value))
    except Exception:
        return default


def get_first(row: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    for key in keys:
        if key in row and row.get(key) not in [None, ""]:
            return row.get(key)
    return default


def round_to_step(value: float, step: int = STRIKE_STEP) -> int:
    return int(round(value / step) * step)


def send_telegram(message: str) -> None:
    """Telegram plain text sender. Falls back to printing if Telegram secrets are missing."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(message)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    chunks = [message[i:i + 3900] for i in range(0, len(message), 3900)]
    for chunk in chunks:
        try:
            resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": chunk}, timeout=15)
            if not resp.ok:
                print("Telegram error:", resp.text)
                print(chunk)
        except Exception as exc:
            print("Telegram send error:", exc)
            print(chunk)


def require_env() -> None:
    missing = []
    for name, val in {
        "FYERS_CLIENT_ID": FYERS_CLIENT_ID,
        "FYERS_ACCESS_TOKEN": FYERS_ACCESS_TOKEN,
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_KEY": SUPABASE_KEY,
    }.items():
        if not val:
            missing.append(name)
    if missing:
        raise RuntimeError("Missing required secrets: " + ", ".join(missing))


# ============================================================
# CLIENTS
# ============================================================

def get_fyers():
    return fyersModel.FyersModel(
        client_id=FYERS_CLIENT_ID,
        token=FYERS_ACCESS_TOKEN,
        is_async=False,
        log_path="",
    )


def get_db():
    return create_client(SUPABASE_URL, SUPABASE_KEY)


# ============================================================
# FYERS MARKET DATA
# ============================================================

def batch_quotes(fyers, symbols: List[str], batch_size: int = 50) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    clean_symbols = [s for s in symbols if s]

    for i in range(0, len(clean_symbols), batch_size):
        batch = clean_symbols[i:i + batch_size]
        try:
            resp = fyers.quotes({"symbols": ",".join(batch)})
        except Exception as exc:
            print("Quote exception:", exc)
            continue

        if resp.get("s") != "ok":
            print("Quote error:", json.dumps(resp)[:1000])
            continue

        for item in resp.get("d", []):
            symbol = item.get("n") or item.get("symbol")
            v = item.get("v", {}) or {}
            if symbol:
                out[symbol] = v
    return out


def quote_number(q: Dict[str, Any], keys: List[str], default: float = 0.0) -> float:
    return to_float(get_first(q, keys, default), default)


def get_spot_and_vix(fyers) -> Tuple[float, float]:
    quotes = batch_quotes(fyers, [UNDERLYING, VIX_SYMBOL])
    nq = quotes.get(UNDERLYING, {})
    vq = quotes.get(VIX_SYMBOL, {})

    spot = quote_number(nq, ["lp", "ltp", "last_price", "prev_close_price"], 0)
    vix = quote_number(vq, ["lp", "ltp", "last_price", "prev_close_price"], 0)

    if spot <= 0:
        raise RuntimeError(f"Could not fetch NIFTY spot. Quote={nq}")
    return spot, vix


def fetch_option_chain(fyers, timestamp: str = "") -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    payload = {"symbol": UNDERLYING, "strikecount": STRIKECOUNT, "timestamp": timestamp or ""}
    resp = fyers.optionchain(data=payload)
    if resp.get("s") != "ok":
        raise RuntimeError("FYERS optionchain failed: " + json.dumps(resp)[:1500])

    data = resp.get("data", {}) or {}
    chain = data.get("optionsChain", []) or []
    expiry_data = data.get("expiryData", []) or []
    return chain, expiry_data, resp


def parse_expiry_date(value: Any) -> Optional[date]:
    if value in [None, ""]:
        return None
    if isinstance(value, (int, float)):
        # FYERS may give epoch seconds for expiry timestamp.
        if value > 1000000000:
            return datetime.fromtimestamp(value, IST).date()
        return None

    text = str(value).strip()
    if text.isdigit():
        num = int(text)
        if num > 1000000000:
            return datetime.fromtimestamp(num, IST).date()
        return None

    try:
        return date_parser.parse(text, dayfirst=True, fuzzy=True).date()
    except Exception:
        return None


def expiry_row_date(row: Dict[str, Any]) -> Optional[date]:
    for key in ["date", "expiry", "expiryDate", "expiry_date", "expiryData"]:
        d = parse_expiry_date(row.get(key))
        if d:
            return d
    return None


def expiry_row_timestamp(row: Dict[str, Any]) -> str:
    for key in ["expiry", "timestamp", "expiry_timestamp", "expiryTimestamp", "expiry_ts"]:
        val = row.get(key)
        if val in [None, ""]:
            continue
        text = str(val)
        if text.isdigit() and int(text) > 1000000000:
            return text
    return ""


def choose_entry_expiry(fyers) -> Tuple[date, str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Pick nearest expiry whose DTE is inside configured weekly entry range."""
    chain, expiry_data, _ = fetch_option_chain(fyers, timestamp="")

    # If expiryData exists, use it to choose the correct weekly expiry.
    for row in expiry_data:
        exp_date = expiry_row_date(row)
        if not exp_date:
            continue
        dte = (exp_date - today_ist()).days
        if ENTRY_DTE_MIN <= dte <= ENTRY_DTE_MAX:
            ts = expiry_row_timestamp(row)
            if ts:
                chain2, expiry_data2, _ = fetch_option_chain(fyers, timestamp=ts)
                return exp_date, ts, chain2, expiry_data2
            return exp_date, "", chain, expiry_data

    # Fallback: use first expiry/date from current response.
    if expiry_data:
        exp_date = expiry_row_date(expiry_data[0])
        ts = expiry_row_timestamp(expiry_data[0])
        if exp_date:
            dte = (exp_date - today_ist()).days
            if ENTRY_DTE_MIN <= dte <= ENTRY_DTE_MAX:
                return exp_date, ts, chain, expiry_data
            raise RuntimeError(f"Entry skipped: nearest expiry DTE={dte}, allowed range={ENTRY_DTE_MIN}-{ENTRY_DTE_MAX}.")

    raise RuntimeError("Could not identify expiry date from FYERS optionchain response.")


def normalize_chain_row(row: Dict[str, Any]) -> Dict[str, Any]:
    symbol = str(get_first(row, ["symbol", "n"], "") or "")
    opt_type = str(get_first(row, ["option_type", "optionType", "type"], "") or "").upper()
    strike = to_float(get_first(row, ["strike_price", "strikePrice", "strike"], 0), 0)

    greeks = row.get("greeks", {}) or {}
    delta = to_float(get_first(row, ["delta"], greeks.get("delta", 0)), 0)

    return {
        "symbol": symbol,
        "type": opt_type,
        "strike": strike,
        "ltp": to_float(get_first(row, ["ltp", "lp", "last_price"], 0), 0),
        "bid": to_float(get_first(row, ["bid", "bid_price", "best_bid_price"], 0), 0),
        "ask": to_float(get_first(row, ["ask", "ask_price", "best_ask_price"], 0), 0),
        "volume": to_int(get_first(row, ["volume", "vol_traded_today", "total_traded_volume"], 0), 0),
        "oi": to_int(get_first(row, ["oi", "open_interest"], 0), 0),
        "delta": delta,
    }


def enrich_rows_with_quotes(fyers, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    symbols = [r["symbol"] for r in rows if r["symbol"].startswith("NSE:")]
    quotes = batch_quotes(fyers, symbols)

    for r in rows:
        q = quotes.get(r["symbol"], {}) or {}
        if not q:
            continue
        r["ltp"] = quote_number(q, ["lp", "ltp", "last_price", "prev_close_price"], r["ltp"])
        r["bid"] = quote_number(q, ["bid", "bid_price", "best_bid_price"], r["bid"])
        r["ask"] = quote_number(q, ["ask", "ask_price", "best_ask_price"], r["ask"])
        r["volume"] = to_int(get_first(q, ["volume", "vol_traded_today", "total_traded_volume"], r["volume"]), r["volume"])
    return rows


def add_liquidity(row: Dict[str, Any]) -> Dict[str, Any]:
    bid = to_float(row.get("bid"), 0)
    ask = to_float(row.get("ask"), 0)
    ltp = to_float(row.get("ltp"), 0)

    if bid > 0 and ask > 0 and ask >= bid:
        spread = ask - bid
        base = ltp if ltp > 0 else (bid + ask) / 2
        spread_pct = spread / base * 100 if base > 0 else 999
    else:
        spread = 999999
        spread_pct = 999

    row["spread"] = spread
    row["spread_pct"] = spread_pct
    return row


def is_liquid(row: Dict[str, Any], strict: bool = True) -> bool:
    add_liquidity(row)
    if not row["symbol"].startswith("NSE:") or row["ltp"] <= 0 or row["bid"] <= 0 or row["ask"] <= 0:
        return False
    if row["ask"] < row["bid"]:
        return False
    if strict and row["volume"] < MIN_VOLUME:
        return False
    if row["spread_pct"] > MAX_SPREAD_PCT and row["spread"] > MAX_ABS_SPREAD:
        return False
    return True


def prepare_option_rows(fyers, chain: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for item in chain:
        r = normalize_chain_row(item)
        if r["type"] in ["CE", "PE"] and r["symbol"].startswith("NSE:") and r["strike"] > 0:
            rows.append(r)
    if not rows:
        raise RuntimeError("No CE/PE rows found in FYERS optionchain.")
    return enrich_rows_with_quotes(fyers, rows)


# ============================================================
# STRIKE SELECTION
# ============================================================

def has_delta(rows: List[Dict[str, Any]]) -> bool:
    return any(abs(to_float(r.get("delta"), 0)) > 0.01 for r in rows)


def choose_short_leg(rows: List[Dict[str, Any]], spot: float, opt_type: str) -> Optional[Dict[str, Any]]:
    liquid = [add_liquidity(dict(r)) for r in rows if r["type"] == opt_type and is_liquid(dict(r), strict=True)]

    if opt_type == "CE":
        otm = [r for r in liquid if r["strike"] > spot]
    else:
        otm = [r for r in liquid if r["strike"] < spot]

    if not otm:
        return None

    # Prefer delta 23-30 if available.
    if has_delta(otm):
        candidates = []
        for r in otm:
            d = abs(to_float(r.get("delta"), 0))
            if SELL_DELTA_MIN <= d <= SELL_DELTA_MAX:
                candidates.append(r)
        if candidates:
            candidates.sort(key=lambda r: (abs(abs(r.get("delta", 0)) - TARGET_DELTA), r["spread_pct"], -r["volume"]))
            return candidates[0]

    # Fallback: choose liquid OTM strike by percent distance.
    if opt_type == "CE":
        candidates = [r for r in otm if spot * (1 + OTM_PCT_MIN) <= r["strike"] <= spot * (1 + OTM_PCT_MAX)]
    else:
        candidates = [r for r in otm if spot * (1 - OTM_PCT_MAX) <= r["strike"] <= spot * (1 - OTM_PCT_MIN)]

    if not candidates:
        # Looser fallback: choose by distance to target 1.5% OTM.
        target = spot * (1.015 if opt_type == "CE" else 0.985)
        otm.sort(key=lambda r: (abs(r["strike"] - target), r["spread_pct"], -r["volume"]))
        return otm[0]

    candidates.sort(key=lambda r: (r["spread_pct"], -r["volume"], abs(r["strike"] - spot)))
    return candidates[0]


def find_leg_by_strike(rows: List[Dict[str, Any]], opt_type: str, strike: float) -> Optional[Dict[str, Any]]:
    exact = [dict(r) for r in rows if r["type"] == opt_type and int(r["strike"]) == int(strike)]
    if not exact:
        return None
    exact.sort(key=lambda r: (0 if is_liquid(dict(r), strict=False) else 1, r.get("spread_pct", 999)))
    return exact[0]


def choose_buy_leg(rows: List[Dict[str, Any]], opt_type: str, sell_strike: float) -> Optional[Dict[str, Any]]:
    target = sell_strike + WING_WIDTH if opt_type == "CE" else sell_strike - WING_WIDTH
    leg = find_leg_by_strike(rows, opt_type, target)
    if leg and is_liquid(dict(leg), strict=False):
        return leg

    # Fallback: nearest further OTM leg.
    if opt_type == "CE":
        candidates = [dict(r) for r in rows if r["type"] == "CE" and r["strike"] > sell_strike]
        candidates.sort(key=lambda r: abs(r["strike"] - target))
    else:
        candidates = [dict(r) for r in rows if r["type"] == "PE" and r["strike"] < sell_strike]
        candidates.sort(key=lambda r: abs(r["strike"] - target))

    for c in candidates:
        if is_liquid(dict(c), strict=False):
            return c
    return candidates[0] if candidates else None


def build_condor(fyers, rows: List[Dict[str, Any]], spot: float) -> Dict[str, Any]:
    sell_ce = choose_short_leg(rows, spot, "CE")
    sell_pe = choose_short_leg(rows, spot, "PE")
    if not sell_ce or not sell_pe:
        raise RuntimeError("Could not select liquid short CE/PE legs.")

    buy_ce = choose_buy_leg(rows, "CE", sell_ce["strike"])
    buy_pe = choose_buy_leg(rows, "PE", sell_pe["strike"])
    if not buy_ce or not buy_pe:
        raise RuntimeError("Could not select protective buy CE/PE legs.")

    legs = {
        "sell_ce": leg_snapshot(sell_ce, "SELL", "CE"),
        "buy_ce": leg_snapshot(buy_ce, "BUY", "CE"),
        "sell_pe": leg_snapshot(sell_pe, "SELL", "PE"),
        "buy_pe": leg_snapshot(buy_pe, "BUY", "PE"),
    }
    price_legs_conservative(legs)

    credit_per_unit = legs["sell_ce"]["entry_price"] + legs["sell_pe"]["entry_price"] - legs["buy_ce"]["entry_price"] - legs["buy_pe"]["entry_price"]
    if credit_per_unit <= 0:
        raise RuntimeError(f"Selected condor has non-positive credit: {credit_per_unit:.2f}")

    call_width = legs["buy_ce"]["strike"] - legs["sell_ce"]["strike"]
    put_width = legs["sell_pe"]["strike"] - legs["buy_pe"]["strike"]
    wing_width = max(call_width, put_width)

    return {
        "legs": legs,
        "credit_per_unit": credit_per_unit,
        "credit_total": credit_per_unit * LOT_SIZE,
        "wing_width": wing_width,
        "max_loss_total": max(0, (wing_width - credit_per_unit) * LOT_SIZE),
        "breakeven_upper": legs["sell_ce"]["strike"] + credit_per_unit,
        "breakeven_lower": legs["sell_pe"]["strike"] - credit_per_unit,
    }


def leg_snapshot(row: Dict[str, Any], side: str, opt_type: str) -> Dict[str, Any]:
    add_liquidity(row)
    return {
        "side": side,
        "type": opt_type,
        "symbol": row["symbol"],
        "strike": float(row["strike"]),
        "bid": float(row.get("bid", 0)),
        "ask": float(row.get("ask", 0)),
        "ltp": float(row.get("ltp", 0)),
        "volume": int(row.get("volume", 0)),
        "oi": int(row.get("oi", 0)),
        "delta": float(row.get("delta", 0)),
        "spread_pct": float(row.get("spread_pct", 999)),
    }


def price_legs_conservative(legs: Dict[str, Dict[str, Any]]) -> None:
    """Paper entry: sell legs at bid, buy legs at ask."""
    for name, leg in legs.items():
        if leg["side"] == "SELL":
            leg["entry_price"] = to_float(leg.get("bid"), 0) or to_float(leg.get("ltp"), 0)
        else:
            leg["entry_price"] = to_float(leg.get("ask"), 0) or to_float(leg.get("ltp"), 0)


# ============================================================
# DATABASE
# ============================================================

def get_active_trade(db) -> Optional[Dict[str, Any]]:
    res = db.table(TABLE_NAME).select("*").eq("status", "ACTIVE").eq("is_paper_trade", True).order("created_at", desc=True).limit(1).execute()
    return res.data[0] if res.data else None


def insert_trade(db, trade: Dict[str, Any]) -> Dict[str, Any]:
    res = db.table(TABLE_NAME).insert(trade).execute()
    if not res.data:
        raise RuntimeError("Supabase insert returned no data.")
    return res.data[0]


def update_trade(db, trade_id: str, updates: Dict[str, Any]) -> None:
    updates["updated_at"] = now_ist().isoformat()
    db.table(TABLE_NAME).update(updates).eq("id", trade_id).execute()


# ============================================================
# MTM / P&L / SETTLEMENT
# ============================================================

def current_leg_price_for_close(leg: Dict[str, Any], quote: Dict[str, Any]) -> float:
    bid = quote_number(quote, ["bid", "bid_price", "best_bid_price"], 0)
    ask = quote_number(quote, ["ask", "ask_price", "best_ask_price"], 0)
    ltp = quote_number(quote, ["lp", "ltp", "last_price", "prev_close_price"], 0)

    # To close: short legs are bought at ask; long legs are sold at bid.
    if leg["side"] == "SELL":
        return ask or ltp
    return bid or ltp


def calculate_mtm(fyers, trade: Dict[str, Any]) -> Dict[str, Any]:
    legs = trade["legs"]
    symbols = [leg["symbol"] for leg in legs.values()]
    quotes = batch_quotes(fyers, symbols)

    leg_rows = {}
    for key, leg in legs.items():
        q = quotes.get(leg["symbol"], {})
        curr = current_leg_price_for_close(leg, q)
        entry = to_float(leg.get("entry_price"), 0)
        if leg["side"] == "SELL":
            pnl_per_unit = entry - curr
        else:
            pnl_per_unit = curr - entry
        leg_rows[key] = {
            "symbol": leg["symbol"],
            "side": leg["side"],
            "type": leg["type"],
            "strike": leg["strike"],
            "entry": entry,
            "current": curr,
            "pnl_per_unit": pnl_per_unit,
            "pnl_total": pnl_per_unit * LOT_SIZE,
        }

    unrealized = sum(x["pnl_total"] for x in leg_rows.values())
    realized = to_float(trade.get("realized_pnl_total"), 0)
    total_pnl = realized + unrealized

    short_close = leg_rows["sell_ce"]["current"] + leg_rows["sell_pe"]["current"]
    long_close = leg_rows["buy_ce"]["current"] + leg_rows["buy_pe"]["current"]
    debit_to_close = short_close - long_close

    return {
        "legs": leg_rows,
        "unrealized_pnl_total": unrealized,
        "realized_pnl_total": realized,
        "total_pnl": total_pnl,
        "debit_to_close_per_unit": debit_to_close,
    }


def settlement_pnl(trade: Dict[str, Any], spot: float) -> float:
    legs = trade["legs"]
    credit = to_float(trade.get("credit_per_unit"), 0)

    sell_ce = legs["sell_ce"]["strike"]
    buy_ce = legs["buy_ce"]["strike"]
    sell_pe = legs["sell_pe"]["strike"]
    buy_pe = legs["buy_pe"]["strike"]

    short_ce = max(spot - sell_ce, 0)
    long_ce = max(spot - buy_ce, 0)
    short_pe = max(sell_pe - spot, 0)
    long_pe = max(buy_pe - spot, 0)

    pnl_per_unit = credit - short_ce + long_ce - short_pe + long_pe
    return to_float(trade.get("realized_pnl_total"), 0) + pnl_per_unit * LOT_SIZE


def append_snapshot(db, trade: Dict[str, Any], spot: float, vix: float, mtm: Dict[str, Any]) -> None:
    snapshots = trade.get("snapshots") or []
    if not isinstance(snapshots, list):
        snapshots = []
    snapshots.append({
        "time": now_ist().isoformat(),
        "spot": spot,
        "vix": vix,
        "unrealized_pnl_total": round(mtm["unrealized_pnl_total"], 2),
        "realized_pnl_total": round(mtm["realized_pnl_total"], 2),
        "total_pnl": round(mtm["total_pnl"], 2),
        "debit_to_close_per_unit": round(mtm["debit_to_close_per_unit"], 2),
    })
    snapshots = snapshots[-500:]
    update_trade(db, trade["id"], {"snapshots": snapshots})


# ============================================================
# ENTRY / ADJUSTMENT / MANAGEMENT
# ============================================================

def entry_flow(db, fyers) -> None:
    active = get_active_trade(db)
    if active:
        send_telegram("Entry skipped: active paper iron condor already exists.")
        return

    spot, vix = get_spot_and_vix(fyers)
    expiry, expiry_ts, chain, _ = choose_entry_expiry(fyers)
    dte = (expiry - today_ist()).days

    rows = prepare_option_rows(fyers, chain)
    condor = build_condor(fyers, rows, spot)

    trade = {
        "status": "ACTIVE",
        "is_paper_trade": True,
        "underlying": UNDERLYING,
        "expiry": expiry.isoformat(),
        "expiry_timestamp": expiry_ts,
        "entry_time": now_ist().isoformat(),
        "entry_spot": spot,
        "entry_vix": vix,
        "lot_size": LOT_SIZE,
        "legs": condor["legs"],
        "credit_per_unit": condor["credit_per_unit"],
        "credit_total": condor["credit_total"],
        "wing_width": condor["wing_width"],
        "max_loss_total": condor["max_loss_total"],
        "breakeven_upper": condor["breakeven_upper"],
        "breakeven_lower": condor["breakeven_lower"],
        "realized_pnl_total": 0,
        "adjustment_count": 0,
        "adjustments_today": 0,
        "last_adjustment_date": None,
        "history": [{
            "time": now_ist().isoformat(),
            "event": "ENTRY",
            "spot": spot,
            "vix": vix,
            "dte": dte,
            "legs": condor["legs"],
            "credit_per_unit": round(condor["credit_per_unit"], 2),
            "credit_total": round(condor["credit_total"], 2),
        }],
        "snapshots": [],
    }
    saved = insert_trade(db, trade)

    send_telegram(format_entry_message(saved, dte))


def should_adjust(trade: Dict[str, Any], spot: float) -> Tuple[bool, str]:
    legs = trade["legs"]
    sell_ce = to_float(legs["sell_ce"]["strike"])
    sell_pe = to_float(legs["sell_pe"]["strike"])
    entry_spot = to_float(trade.get("entry_spot"), spot)

    # Prevent too many adjustments.
    if to_int(trade.get("adjustment_count"), 0) >= MAX_TOTAL_ADJUSTMENTS:
        return False, "max total adjustments reached"

    today_s = today_ist().isoformat()
    adjustments_today = to_int(trade.get("adjustments_today"), 0)
    if trade.get("last_adjustment_date") == today_s and adjustments_today >= MAX_ADJUSTMENTS_PER_DAY:
        return False, "daily adjustment limit reached"

    if abs(spot - entry_spot) >= RECENTER_MOVE:
        return True, "large move recenter"
    if spot >= sell_ce - TRIGGER_DISTANCE:
        return True, "call side tested"
    if spot <= sell_pe + TRIGGER_DISTANCE:
        return True, "put side tested"

    return False, "safe"


def adjustment_flow(db, fyers, trade: Dict[str, Any], spot: float, vix: float, reason: str, mtm: Dict[str, Any]) -> None:
    expiry_ts = trade.get("expiry_timestamp") or ""
    chain, _, _ = fetch_option_chain(fyers, timestamp=expiry_ts)
    rows = prepare_option_rows(fyers, chain)
    new_condor = build_condor(fyers, rows, spot)

    realized_before = to_float(trade.get("realized_pnl_total"), 0)
    realized_after = realized_before + mtm["unrealized_pnl_total"]

    history = trade.get("history") or []
    if not isinstance(history, list):
        history = []
    history.append({
        "time": now_ist().isoformat(),
        "event": "ADJUSTMENT",
        "reason": reason,
        "spot": spot,
        "vix": vix,
        "closed_unrealized_pnl_total": round(mtm["unrealized_pnl_total"], 2),
        "realized_pnl_total_after_close": round(realized_after, 2),
        "old_legs": trade["legs"],
        "new_legs": new_condor["legs"],
        "new_credit_per_unit": round(new_condor["credit_per_unit"], 2),
    })

    today_s = today_ist().isoformat()
    if trade.get("last_adjustment_date") == today_s:
        adjustments_today = to_int(trade.get("adjustments_today"), 0) + 1
    else:
        adjustments_today = 1

    updates = {
        "legs": new_condor["legs"],
        "credit_per_unit": new_condor["credit_per_unit"],
        "credit_total": new_condor["credit_total"],
        "wing_width": new_condor["wing_width"],
        "max_loss_total": new_condor["max_loss_total"],
        "breakeven_upper": new_condor["breakeven_upper"],
        "breakeven_lower": new_condor["breakeven_lower"],
        "realized_pnl_total": realized_after,
        "adjustment_count": to_int(trade.get("adjustment_count"), 0) + 1,
        "adjustments_today": adjustments_today,
        "last_adjustment_date": today_s,
        "history": history,
    }
    update_trade(db, trade["id"], updates)

    send_telegram(format_adjustment_message(reason, spot, mtm, new_condor, realized_after))


def management_flow(db, fyers, trade: Dict[str, Any]) -> None:
    spot, vix = get_spot_and_vix(fyers)
    mtm = calculate_mtm(fyers, trade)
    append_snapshot(db, trade, spot, vix, mtm)

    expiry = date_parser.parse(trade["expiry"]).date()
    settlement_time = time(SETTLE_AFTER_HOUR, SETTLE_AFTER_MINUTE)
    if today_ist() >= expiry and now_ist().time() >= settlement_time:
        final_pnl = settlement_pnl(trade, spot)
        history = trade.get("history") or []
        if not isinstance(history, list):
            history = []
        history.append({
            "time": now_ist().isoformat(),
            "event": "EXPIRY_SETTLEMENT",
            "spot": spot,
            "final_pnl_total": round(final_pnl, 2),
        })
        update_trade(db, trade["id"], {
            "status": "EXPIRED",
            "exit_time": now_ist().isoformat(),
            "exit_reason": "EXPIRY_SETTLEMENT",
            "final_pnl_total": final_pnl,
            "history": history,
        })
        send_telegram(format_settlement_message(trade, spot, final_pnl))
        return

    adjust, reason = should_adjust(trade, spot)
    if adjust:
        adjustment_flow(db, fyers, trade, spot, vix, reason, mtm)
    else:
        send_telegram(format_mtm_message(trade, spot, vix, mtm, reason))


# ============================================================
# TELEGRAM FORMATTERS
# ============================================================

def leg_line(name: str, leg: Dict[str, Any]) -> str:
    return f"{name:<7} {leg['type']} {int(leg['strike'])} @ {to_float(leg.get('entry_price')):.2f}"


def format_entry_message(trade: Dict[str, Any], dte: int) -> str:
    legs = trade["legs"]
    return (
        "NEW PAPER IRON CONDOR\n"
        "-----------------------\n"
        f"Underlying: NIFTY\n"
        f"Spot: {trade['entry_spot']:.2f} | VIX: {trade['entry_vix']:.2f}\n"
        f"Expiry: {trade['expiry']} | DTE: {dte}\n\n"
        f"{leg_line('SELL', legs['sell_ce'])}\n"
        f"{leg_line('BUY', legs['buy_ce'])}\n"
        f"{leg_line('SELL', legs['sell_pe'])}\n"
        f"{leg_line('BUY', legs['buy_pe'])}\n\n"
        f"Credit/unit: {trade['credit_per_unit']:.2f}\n"
        f"Credit/lot: {trade['credit_total']:.2f}\n"
        f"Max loss/lot: {trade['max_loss_total']:.2f}\n"
        f"Breakeven: {trade['breakeven_lower']:.2f} - {trade['breakeven_upper']:.2f}\n"
        "Mode: PAPER ONLY, no real order placed."
    )


def format_mtm_message(trade: Dict[str, Any], spot: float, vix: float, mtm: Dict[str, Any], reason: str) -> str:
    lines = [
        "IRON CONDOR MTM UPDATE",
        "----------------------",
        f"Spot: {spot:.2f} | VIX: {vix:.2f}",
        f"Expiry: {trade['expiry']}",
        f"Realized P&L: {mtm['realized_pnl_total']:.2f}",
        f"Unrealized P&L: {mtm['unrealized_pnl_total']:.2f}",
        f"Total P&L: {mtm['total_pnl']:.2f}",
        f"Debit to close/unit: {mtm['debit_to_close_per_unit']:.2f}",
        f"Status: no adjustment ({reason})",
        "",
        "Legs:",
    ]
    for key in ["sell_ce", "buy_ce", "sell_pe", "buy_pe"]:
        x = mtm["legs"][key]
        lines.append(f"{key.upper():<8} {int(x['strike'])}: {x['entry']:.2f} -> {x['current']:.2f} | P&L {x['pnl_total']:+.2f}")
    return "\n".join(lines)


def format_adjustment_message(reason: str, spot: float, mtm: Dict[str, Any], new_condor: Dict[str, Any], realized_after: float) -> str:
    legs = new_condor["legs"]
    return (
        "IRON CONDOR ADJUSTMENT\n"
        "----------------------\n"
        f"Reason: {reason}\n"
        f"Spot: {spot:.2f}\n"
        f"Closed old condor P&L: {mtm['unrealized_pnl_total']:+.2f}\n"
        f"Realized P&L after close: {realized_after:+.2f}\n\n"
        "New paper condor:\n"
        f"{leg_line('SELL', legs['sell_ce'])}\n"
        f"{leg_line('BUY', legs['buy_ce'])}\n"
        f"{leg_line('SELL', legs['sell_pe'])}\n"
        f"{leg_line('BUY', legs['buy_pe'])}\n\n"
        f"New credit/unit: {new_condor['credit_per_unit']:.2f}\n"
        f"New BE: {new_condor['breakeven_lower']:.2f} - {new_condor['breakeven_upper']:.2f}"
    )


def format_settlement_message(trade: Dict[str, Any], spot: float, final_pnl: float) -> str:
    return (
        "EXPIRY SETTLEMENT\n"
        "-----------------\n"
        f"Expiry: {trade['expiry']}\n"
        f"Settlement spot used: {spot:.2f}\n"
        f"Final paper P&L: {final_pnl:+.2f}\n"
        "Trade marked EXPIRED."
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    try:
        print("RUNNING WEEKLY IRON CONDOR PAPER BOT v1")
        require_env()
        fyers = get_fyers()
        db = get_db()

        active = get_active_trade(db)
        if active:
            management_flow(db, fyers, active)
        else:
            entry_flow(db, fyers)

    except Exception as exc:
        err = traceback.format_exc()
        print(err)
        send_telegram("BOT ERROR\n---------\n" + repr(exc)[:1500])


if __name__ == "__main__":
    main()
