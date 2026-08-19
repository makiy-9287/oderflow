"""Footprint / order flow: trade tape, bar delta, CVD, absorption, divergence.

SIGN CONVENTION — the single most error-prone line in this codebase.
Binance aggTrade carries `m` = "is buyer the maker". If `m is True` the buyer
was passive, so the SELLER lifted, and the trade is SELL aggression. ccxt
already normalises this into `trade["side"]`, where "buy" means the taker
bought. We read `info["m"]` when present and fall back to `side`, so the
convention is fixed in exactly one place: `_is_buy_taker`.

WARMUP — delta cannot be reconstructed from candles. It requires the trade
tape. Until `min_ready_bars` complete buckets have accumulated, `analyse_flow`
returns `ready=False`, every flow confirmation scores zero, and because L3
demands two confirmations a symbol simply cannot signal. That is intentional:
no invented flow data, ever.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from ofsignals.types import Direction, FlowSnapshot


@dataclass(slots=True)
class FootprintBar:
    start_ms: int
    open: float
    high: float
    low: float
    close: float
    buy_volume: float
    sell_volume: float

    @property
    def volume(self) -> float:
        return self.buy_volume + self.sell_volume

    @property
    def delta(self) -> float:
        return self.buy_volume - self.sell_volume

    @property
    def range(self) -> float:
        return self.high - self.low


def _is_buy_taker(trade: dict[str, Any]) -> bool:
    info = trade.get("info") or {}
    maker_flag = info.get("m")
    if maker_flag is not None:
        if isinstance(maker_flag, str):
            return maker_flag.lower() not in ("true", "1")
        return not bool(maker_flag)
    return str(trade.get("side", "buy")).lower() == "buy"


class TradeTape:
    """Bounded ring buffer of raw taker prints for one symbol."""

    __slots__ = ("symbol", "_trades", "_last_ms")

    def __init__(self, symbol: str, maxlen: int = 30_000) -> None:
        self.symbol = symbol
        self._trades: deque[tuple[int, float, float, bool]] = deque(maxlen=maxlen)
        self._last_ms = 0

    def __len__(self) -> int:
        return len(self._trades)

    @property
    def last_ms(self) -> int:
        return self._last_ms

    def add_many(self, trades: Iterable[dict[str, Any]]) -> int:
        added = 0
        for trade in trades:
            ts = trade.get("timestamp")
            price = trade.get("price")
            amount = trade.get("amount")
            if ts is None or price is None or amount is None:
                continue
            ts = int(ts)
            if ts < self._last_ms - 60_000:      # tolerate small out-of-order, drop stale
                continue
            self._trades.append((ts, float(price), float(amount), _is_buy_taker(trade)))
            self._last_ms = max(self._last_ms, ts)
            added += 1
        return added

    def bars(self, bucket_ms: int, count: int = 60,
             now_ms: int | None = None) -> list[FootprintBar]:
        """Aggregate the tape into closed buckets, oldest first.

        The bucket containing `now_ms` is still forming and is excluded.
        """
        if not self._trades or bucket_ms <= 0:
            return []
        now_ms = now_ms or self._last_ms
        current_bucket = (now_ms // bucket_ms) * bucket_ms
        oldest_allowed = current_bucket - bucket_ms * count

        buckets: dict[int, FootprintBar] = {}
        for ts, price, amount, is_buy in self._trades:
            key = (ts // bucket_ms) * bucket_ms
            if key >= current_bucket or key < oldest_allowed:
                continue
            bar = buckets.get(key)
            if bar is None:
                buckets[key] = FootprintBar(
                    key, price, price, price, price,
                    amount if is_buy else 0.0, 0.0 if is_buy else amount,
                )
            else:
                bar.high = max(bar.high, price)
                bar.low = min(bar.low, price)
                bar.close = price
                if is_buy:
                    bar.buy_volume += amount
                else:
                    bar.sell_volume += amount
        return [buckets[k] for k in sorted(buckets)]


# --------------------------------------------------------------------- analysis
def cumulative_delta(bars: list[FootprintBar]) -> np.ndarray:
    if not bars:
        return np.array([])
    return np.cumsum(np.array([b.delta for b in bars], dtype=np.float64))


def analyse_flow(tape: TradeTape, bucket_ms: int, direction: Direction, cfg: dict,
                 poi_low: float | None = None, poi_high: float | None = None,
                 atr_value: float | None = None,
                 min_ready_bars: int = 12) -> FlowSnapshot:
    """L3 of the cascade: did aggressive flow get absorbed?"""
    lookback = int(cfg.get("cvd_lookback_bars", 96))
    bars = tape.bars(bucket_ms, count=lookback)

    if len(bars) < min_ready_bars:
        return FlowSnapshot(False, 0.0, 0.0, False, False, False, False,
                            f"tape warming up ({len(bars)}/{min_ready_bars} bars)")

    cvd = cumulative_delta(bars)
    lows = np.array([b.low for b in bars])
    highs = np.array([b.high for b in bars])
    volumes = np.array([b.volume for b in bars])
    ranges = np.array([b.range for b in bars])
    deltas = np.array([b.delta for b in bars])

    min_separation = int(cfg.get("divergence_min_bars", 3))
    divergence = _detect_divergence(lows, highs, cvd, direction, min_separation)

    absorption_cfg = cfg.get("absorption", {}) or {}
    absorption = _detect_absorption(
        bars, volumes, ranges,
        volume_mult=float(absorption_cfg.get("volume_mult", 2.0)),
        range_atr_mult=float(absorption_cfg.get("range_atr_mult", 0.5)),
        atr_value=atr_value, poi_low=poi_low, poi_high=poi_high,
    )

    sign = direction.sign
    recent = deltas[-2:]
    delta_flip = bool(sign != 0 and len(recent) == 2 and np.all(np.sign(recent) == sign))

    window = deltas[-20:] if deltas.size >= 20 else deltas
    if sign > 0:
        extreme_index = int(window.argmin())
    elif sign < 0:
        extreme_index = int(window.argmax())
    else:
        extreme_index = -1
    delta_extreme = bool(extreme_index >= max(0, window.size - 4))

    return FlowSnapshot(
        ready=True,
        cvd=float(cvd[-1]),
        bar_delta=float(deltas[-1]),
        cvd_divergence=divergence,
        absorption=absorption,
        delta_flip=delta_flip,
        delta_extreme=delta_extreme,
        note=f"{len(bars)} footprint bars",
    )


def _detect_divergence(lows: np.ndarray, highs: np.ndarray, cvd: np.ndarray,
                       direction: Direction, min_separation: int) -> bool:
    """Price makes a new extreme; cumulative delta refuses to follow."""
    n = lows.size
    if n < min_separation + 4 or cvd.size != n:
        return False

    recent = slice(max(0, n - 4), n)
    prior_end = n - 4
    prior_start = max(0, prior_end - 20)
    if prior_end - prior_start < min_separation:
        return False

    if direction is Direction.LONG:
        recent_index = int(np.argmin(lows[recent])) + max(0, n - 4)
        prior_index = int(np.argmin(lows[prior_start:prior_end])) + prior_start
        if recent_index - prior_index < min_separation:
            return False
        price_lower = lows[recent_index] < lows[prior_index]
        cvd_higher = cvd[recent_index] > cvd[prior_index]
        return bool(price_lower and cvd_higher)

    if direction is Direction.SHORT:
        recent_index = int(np.argmax(highs[recent])) + max(0, n - 4)
        prior_index = int(np.argmax(highs[prior_start:prior_end])) + prior_start
        if recent_index - prior_index < min_separation:
            return False
        price_higher = highs[recent_index] > highs[prior_index]
        cvd_lower = cvd[recent_index] < cvd[prior_index]
        return bool(price_higher and cvd_lower)

    return False


def _detect_absorption(bars: list[FootprintBar], volumes: np.ndarray, ranges: np.ndarray,
                       volume_mult: float, range_atr_mult: float,
                       atr_value: float | None, poi_low: float | None,
                       poi_high: float | None) -> bool:
    """Heavy volume, compressed range, at the level that matters."""
    if volumes.size < 6:
        return False
    average = float(volumes[:-1].mean())
    if average <= 0:
        return False

    range_cap = (atr_value * range_atr_mult) if atr_value and atr_value > 0 else None

    for bar, volume, bar_range in zip(bars[-4:], volumes[-4:], ranges[-4:]):
        if volume < average * volume_mult:
            continue
        if range_cap is not None and bar_range >= range_cap:
            continue
        if range_cap is None and bar_range > float(np.median(ranges)) * 0.8:
            continue
        if poi_low is not None and poi_high is not None:
            if bar.low > poi_high or bar.high < poi_low:
                continue        # heavy volume, but not where the setup lives
        return True
    return False
