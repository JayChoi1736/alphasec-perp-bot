"""Reusable one-tick strategy steps, decoupled from any loop or pacing.

The same maker/taker action drives a standalone bot (maker.py/taker.py), the
matched-load harness (match.py), or a future locust `@task`. Each `*_step` reads
what it needs from the client and places one tick's orders; the CALLER owns the
loop, the sleep, and the pacing.
"""
import math

from lib.dex import BUY, SELL, IOC, POST, band_bounds


def q(x, step):
    """Quantize to a tick/lot grid; the 12-dp round kills float noise."""
    return round(round(x / step) * step, 12) if step else x


def clamp_band(px, tick, lo, hi):
    """Clamp a price strictly inside the mark band [lo, hi] (both edges)."""
    if lo:
        px = min(max(px, math.ceil(lo / tick) * tick + tick), math.floor(hi / tick) * tick - tick)
    return px


def taker_price(side, ref, slip, tick, lo, hi):
    """IOC cross price: past the touch by `slip`, clamped to the near band edge only."""
    if side == BUY:
        px = q(ref * (1 + slip), tick)
        return min(px, math.floor(hi / tick) * tick - tick) if hi else px
    px = q(ref * (1 - slip), tick)
    return max(px, math.ceil(lo / tick) * tick + tick) if lo else px


def maker_prices(ref, spread, step, levels, tick):
    """Raw (bids, asks) ladders around ref; caller may band-clamp. off = spread + k*step."""
    bids = [q(ref * (1 - (spread + k * step)), tick) for k in range(levels)]
    asks = [q(ref * (1 + (spread + k * step)), tick) for k in range(levels)]
    return bids, asks


def taker_step(c, mid, size, slip, tick, band_bps, i, ref_price, wait=True):
    """One alternating IOC cross (BUY on even i, SELL on odd), clamped to the band.
    Returns (side, px, mark)."""
    mark = c.mark_price(mid)
    bb, ba = c.best_bid_ask(mid)
    lo, hi = band_bounds(mark, band_bps)
    if i % 2 == 0:
        side, ref = BUY, (ba or bb or mark or ref_price)
    else:
        side, ref = SELL, (bb or ba or mark or ref_price)
    px = taker_price(side, ref, slip, tick, lo, hi)
    c.order(mid, side, px, size, tif=IOC, wait=wait)
    return side, px, mark


def maker_step(c, mid, size, spread, step, levels, tick, band_bps, ref_price, state, wait=True):
    """(Re)quote a `levels`-deep ladder around mid, clamped to the band. Skips the
    requote while mid is quiet (< half a level step of drift). `state` is a dict
    holding 'last_mid'. Returns (mark, mid_px) on requote, or None when skipped."""
    mark = c.mark_price(mid)
    bb, ba = c.best_bid_ask(mid)
    mid_px = (bb + ba) / 2 if bb and ba else (bb or ba or mark or ref_price)
    last = state.get("last_mid")
    if last is not None and abs(mid_px - last) < mid_px * step * 0.5:
        return None
    lo, hi = band_bounds(mark, band_bps)
    c.cancel_all(mid, wait=wait)
    bids, asks = maker_prices(mid_px, spread, step, levels, tick)
    for b, a in zip(bids, asks):
        c.order(mid, BUY, clamp_band(b, tick, lo, hi), size, tif=POST, wait=wait)
        c.order(mid, SELL, clamp_band(a, tick, lo, hi), size, tif=POST, wait=wait)
    state["last_mid"] = mid_px
    return mark, mid_px
