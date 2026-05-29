"""
EMA-Regime strategy (v2).

Layers on top of the v1 regime trigger:

  • Time-of-day filter   : skip the first 15 minutes of open (high noise)
                           and anytime past `no_entry_after` (already gated
                           in the risk engine, but defensive here too).
  • RSI confirmation     : don't buy CE if the latest RSI14 on this
                           underlying is overbought (>70).
                           Don't buy PE if RSI14 is oversold (<30).
  • ATR-based stop       : suggested_sl_pct is sized off the latest
                           ATR% (capped to a sensible band).
  • Cooldown             : per-underlying, defends against label flapping.

State maintained per underlying symbol:
  - last_signal_at
  - latest RSI14 (from IndicatorUpdate stream)
  - latest ATR14 (from IndicatorUpdate stream, normalized to % of close)
  - latest close approximation via EMA20
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Final

import pytz

from src.core.events import (
    IndicatorUpdate,
    OptionType,
    RegimeLabel,
    RegimeUpdate,
    SignalCandidate,
    Side,
)
from src.core.logging import get_logger
from src.strategies.base import Strategy

log = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")

DIRECTION_MAP: Final[dict[RegimeLabel, OptionType]] = {
    RegimeLabel.TRENDING_UP: OptionType.CE,
    RegimeLabel.TRENDING_DOWN: OptionType.PE,
}

# Filter thresholds
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0
# Skip first N minutes after market open (9:15 IST) — noise zone
SKIP_FIRST_MINUTES = 15
# ATR-derived stop loss bounds (% of premium)
SL_PCT_MIN, SL_PCT_MAX = 15.0, 40.0
TP_TO_SL_RATIO = 2.0  # 1:2 risk:reward


@dataclass
class _UnderlyingState:
    last_signal_at: datetime | None = None
    rsi14: float | None = None
    atr14: float | None = None
    ema20: float | None = None     # used as a close proxy (1m resolution)
    # Multi-timeframe confirmation: latest EMA20/50 spread sign on coarser bars.
    # +1 = ema20 > ema50 (bullish), -1 = ema20 < ema50, 0 = unknown.
    htf_trend_5m: int = 0
    htf_trend_15m: int = 0
    # Per-resolution EMA snapshots used to recompute the spread sign incrementally.
    _ema_per_res: dict = field(default_factory=dict)
    extras: dict = field(default_factory=dict)


def _market_open_ist(ts: datetime) -> datetime:
    """Today's 09:15 IST as a tz-aware datetime."""
    ist = ts.astimezone(IST)
    return ist.replace(hour=9, minute=15, second=0, microsecond=0)


class EMARegimeStrategy(Strategy):
    id = "ema_regime_v2"

    def __init__(self, cooldown_seconds: int = 30 * 60) -> None:
        self.cooldown = timedelta(seconds=cooldown_seconds)
        self._state: dict[str, _UnderlyingState] = {}

    # ----------------- indicator side: just track state -----------------

    def on_indicator(self, update: IndicatorUpdate) -> SignalCandidate | None:
        st = self._state.setdefault(update.symbol, _UnderlyingState())
        if update.value is None:
            return None

        # Track 1m-resolution scalars used by RSI / ATR / SL sizing
        if update.resolution == "1minute":
            if update.name == "rsi14":
                st.rsi14 = update.value
            elif update.name == "atr14":
                st.atr14 = update.value
            elif update.name == "ema20":
                st.ema20 = update.value

        # Multi-timeframe trend confirmation:
        # store EMA20 and EMA50 per (resolution) and derive the sign.
        if update.name in ("ema20", "ema50"):
            res_state = st._ema_per_res.setdefault(update.resolution, {})
            res_state[update.name] = update.value
            ema20 = res_state.get("ema20")
            ema50 = res_state.get("ema50")
            if ema20 is not None and ema50 is not None:
                sign = 1 if ema20 > ema50 else (-1 if ema20 < ema50 else 0)
                if update.resolution == "5minute":
                    st.htf_trend_5m = sign
                elif update.resolution == "15minute":
                    st.htf_trend_15m = sign
        return None  # state-only; no signals emitted here

    # ----------------- regime side: actual entry trigger -----------------

    def on_regime(self, update: RegimeUpdate) -> SignalCandidate | None:
        opt_type = DIRECTION_MAP.get(update.label)
        if opt_type is None:
            return None

        # --- time-of-day filter ---
        ist = update.ts.astimezone(IST)
        if ist.weekday() >= 5:
            return None  # weekend
        market_open = _market_open_ist(update.ts)
        if ist < market_open + timedelta(minutes=SKIP_FIRST_MINUTES):
            log.debug(
                "strategy_skip_warmup_window",
                strategy=self.id, underlying=update.symbol, ist_time=ist.strftime("%H:%M"),
            )
            return None

        # --- cooldown ---
        st = self._state.setdefault(update.symbol, _UnderlyingState())
        if st.last_signal_at is not None and (update.ts - st.last_signal_at) < self.cooldown:
            log.debug(
                "strategy_skip_cooldown",
                strategy=self.id, underlying=update.symbol,
                remaining_s=int(
                    (st.last_signal_at + self.cooldown - update.ts).total_seconds()
                ),
            )
            return None

        # --- RSI confirmation ---
        if st.rsi14 is not None:
            if opt_type == OptionType.CE and st.rsi14 >= RSI_OVERBOUGHT:
                log.info(
                    "strategy_skip_rsi_overbought",
                    strategy=self.id, underlying=update.symbol, rsi=round(st.rsi14, 2),
                )
                return None
            if opt_type == OptionType.PE and st.rsi14 <= RSI_OVERSOLD:
                log.info(
                    "strategy_skip_rsi_oversold",
                    strategy=self.id, underlying=update.symbol, rsi=round(st.rsi14, 2),
                )
                return None

        # --- Multi-timeframe trend confirmation ---
        # The 5m + 15m EMA spread sign must agree with the trade direction.
        # If either higher timeframe is unknown (no warmup yet), we don't block —
        # better to take some signals during warmup than to be silent for an hour.
        want_sign = 1 if opt_type == OptionType.CE else -1
        if st.htf_trend_5m != 0 and st.htf_trend_5m != want_sign:
            log.info(
                "strategy_skip_htf_5m_disagrees",
                strategy=self.id, underlying=update.symbol,
                htf_5m=st.htf_trend_5m, wanted=want_sign,
            )
            return None
        if st.htf_trend_15m != 0 and st.htf_trend_15m != want_sign:
            log.info(
                "strategy_skip_htf_15m_disagrees",
                strategy=self.id, underlying=update.symbol,
                htf_15m=st.htf_trend_15m, wanted=want_sign,
            )
            return None

        # --- ATR-based stop loss sizing ---
        # Translate ATR% of close to a premium-loss % via a heuristic multiplier.
        # Long-option premia move ~2× the underlying ATR over short timeframes
        # for ATM options (rough rule of thumb; refine with Greeks later).
        sl_pct = 25.0  # default if ATR unknown
        if st.atr14 is not None and st.ema20 and st.ema20 > 0:
            atr_pct = st.atr14 / st.ema20 * 100  # ATR as % of underlying price
            premium_move_pct = atr_pct * 8.0     # ATM-ish premium beta ≈ 8×
            sl_pct = max(SL_PCT_MIN, min(SL_PCT_MAX, premium_move_pct))
        tp_pct = sl_pct * TP_TO_SL_RATIO

        st.last_signal_at = update.ts

        confidence = max(0.0, min(100.0, update.score * 100.0))
        rationale = [
            f"regime={update.label.value}",
            f"score={round(update.score, 3)}",
            f"resolution={update.resolution}",
            f"sl_pct={sl_pct:.1f}",
            f"tp_pct={tp_pct:.1f}",
        ]
        if st.rsi14 is not None:
            rationale.append(f"rsi14={round(st.rsi14, 2)}")
        if st.atr14 is not None:
            rationale.append(f"atr14={round(st.atr14, 2)}")
        if "spread_pct" in update.rationale:
            rationale.append(f"ema_spread_pct={round(float(update.rationale['spread_pct']), 5)}")

        snap = dict(update.rationale)
        if st.rsi14 is not None:
            snap["rsi14"] = st.rsi14
        if st.atr14 is not None:
            snap["atr14"] = st.atr14
        if st.ema20 is not None:
            snap["ema20"] = st.ema20

        return SignalCandidate(
            event_id=str(uuid.uuid4()),
            ts=update.ts,
            strategy_id=self.id,
            underlying=update.symbol,
            side=Side.BUY,
            option_type=opt_type,
            confidence=confidence,
            rationale=rationale,
            indicators_snapshot=snap,
            suggested_sl_pct=sl_pct,
            suggested_target_pct=tp_pct,
        )
