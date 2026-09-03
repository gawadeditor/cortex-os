"""
hypogeum/model_effective_pricing.py — what each provider model ACTUALLY
costs, from real charge history.

The listed / catalogue price (model_registry.price_usd, a channel's adopted
ModelProfile price, the static MODEL_CREDIT_COSTS table) is what Cortex
*books*. It can be wrong — a MUAPI model with dynamic_pricing lists a floor,
not the price we pay at production settings (observed 2026-09-03: wan2.7
listed $0.10, billed $0.65 on every one of 53 calls).

This module reads the two spend ledgers and reports the effective per-call
price actually charged:
  - run_authorization_grant_consumption.charged_amount  (ADR-016 path)
  - costs.cost_usd                                       (pre-authorization path)

Public API:
    effective_prices(*, db_path=None, since=None) -> dict[str, ModelSpend]
    effective_price(model_id, *, db_path=None) -> Optional[float]
    price_view(model_id, listed, *, db_path=None) -> dict   (for templates)
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger

log = get_logger(agent="model_effective_pricing")

# effective vs listed differ by more than this fraction -> flag it
_DRIFT_THRESHOLD = 0.25


@dataclass
class ModelSpend:
    model_id: str
    n_calls: int = 0
    total_charged: float = 0.0
    effective_price: Optional[float] = None      # representative per-call charge
    min_charge: Optional[float] = None
    max_charge: Optional[float] = None
    last_charged_at: Optional[str] = None
    sources: list = field(default_factory=list)   # ["grant", "costs"]

    def as_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "n_calls": self.n_calls,
            "total_charged": round(self.total_charged, 4),
            "effective_price": (round(self.effective_price, 4)
                                if self.effective_price is not None else None),
            "min_charge": self.min_charge,
            "max_charge": self.max_charge,
            "last_charged_at": self.last_charged_at,
            "sources": self.sources,
        }


def _representative(charges: list[float]) -> Optional[float]:
    """A single number that best represents what a call costs. Most provider
    models bill a flat rate, so the mode is ideal; fall back to median."""
    if not charges:
        return None
    rounded = [round(x, 4) for x in charges if x is not None and x > 0]
    if not rounded:
        return None
    try:
        return float(statistics.mode(rounded))
    except statistics.StatisticsError:
        return float(statistics.median(rounded))


def effective_prices(*, db_path: Optional[Path] = None,
                     since: Optional[str] = None) -> dict[str, ModelSpend]:
    """{model_id: ModelSpend} across both ledgers. Never raises."""
    out: dict[str, ModelSpend] = {}
    charges: dict[str, list] = {}

    try:
        from hypogeum.db import get_connection
        conn = get_connection(Path(db_path) if db_path else None)
        try:
            # ADR-016 grant path — charged_amount is TEXT
            gq = ("SELECT model, charged_amount, updated_at, created_at "
                  "FROM run_authorization_grant_consumption "
                  "WHERE model IS NOT NULL AND charged_amount IS NOT NULL")
            gp: list = []
            if since:
                gq += " AND created_at >= ?"
                gp.append(since)
            for r in conn.execute(gq, gp):
                _accum(out, charges, r["model"], r["charged_amount"],
                       r["updated_at"] or r["created_at"], "grant")

            # pre-authorization costs ledger
            cq = ("SELECT model, cost_usd, called_at FROM costs "
                  "WHERE model IS NOT NULL AND cost_usd IS NOT NULL AND cost_usd > 0")
            cp: list = []
            if since:
                cq += " AND called_at >= ?"
                cp.append(since)
            for r in conn.execute(cq, cp):
                _accum(out, charges, r["model"], r["cost_usd"], r["called_at"], "costs")
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warn("effective_prices_failed", error=str(exc))
        return {}

    for mid, spend in out.items():
        cl = charges.get(mid, [])
        spend.effective_price = _representative(cl)
        pos = [c for c in cl if c and c > 0]
        spend.min_charge = round(min(pos), 4) if pos else None
        spend.max_charge = round(max(pos), 4) if pos else None
    return out


def _accum(out, charges, model, raw_amount, at, source):  # noqa: ANN001
    try:
        amt = float(raw_amount)
    except (TypeError, ValueError):
        return
    s = out.get(model)
    if s is None:
        s = out[model] = ModelSpend(model_id=model)
        charges[model] = []
    s.n_calls += 1
    s.total_charged += max(amt, 0.0)
    charges[model].append(amt)
    if source not in s.sources:
        s.sources.append(source)
    if at and (s.last_charged_at is None or str(at) > s.last_charged_at):
        s.last_charged_at = str(at)


# states in run_authorization_grant_consumption that never represent real spend
_NON_SPEND_STATES = ("released", "voided", "refunded", "cancelled", "reserved")


def episode_billed_spend(
    *, db_path: Optional[Path] = None,
    episode_ids: Optional[list[str]] = None,
) -> dict[str, float]:
    """{episode_id: total_usd} — what each episode ACTUALLY cost, all providers.

    The `costs` table only ever recorded the pre-authorization LLM + audio
    calls; it has never held a single MUAPI (video / upscale) charge, so a
    dashboard that sums `costs` shows ~20% of a real episode bill (observed
    2026-09-03: Baby Owl $2.10 shown vs $11.50 billed). For any episode that
    went through the ADR-016 grant path, run_authorization_grant_consumption
    is the complete ledger (elevenlabs + muapi + openrouter + perplexity);
    fall back to the `costs` sum only for older episodes with no grant rows.

    Keyed off the `ep_<id>:<...>` prefix both ledgers use. Never raises."""
    out: dict[str, float] = {}
    try:
        from hypogeum.db import get_connection
        conn = get_connection(Path(db_path) if db_path else None)
        try:
            want = set(episode_ids) if episode_ids else None
            state_filter = ",".join("?" for _ in _NON_SPEND_STATES)
            try:
                grant_rows = conn.execute(
                    f"""
                    SELECT idempotency_key, charged_amount, reserved_amount
                    FROM run_authorization_grant_consumption
                    WHERE idempotency_key LIKE 'ep\\_%' ESCAPE '\\'
                      AND COALESCE(state, '') NOT IN ({state_filter})
                    """,
                    _NON_SPEND_STATES,
                ).fetchall()
            except Exception:  # noqa: BLE001 - table absent in a bare schema
                grant_rows = []
            has_grant: set[str] = set()
            for r in grant_rows:
                ep = str(r["idempotency_key"]).split(":", 1)[0]
                if want is not None and ep not in want:
                    continue
                amt = _num(r["charged_amount"])
                if amt <= 0:
                    amt = _num(r["reserved_amount"])
                out[ep] = out.get(ep, 0.0) + max(amt, 0.0)
                has_grant.add(ep)

            cost_rows = conn.execute(
                "SELECT episode_id, COALESCE(SUM(cost_usd), 0.0) AS s "
                "FROM costs WHERE episode_id IS NOT NULL GROUP BY episode_id"
            ).fetchall()
            for r in cost_rows:
                ep = r["episode_id"]
                if want is not None and ep not in want:
                    continue
                if ep in has_grant:
                    continue  # grant ledger is authoritative + complete
                out[ep] = out.get(ep, 0.0) + _num(r["s"])
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warn("episode_billed_spend_failed", error=str(exc))
        return out
    return {k: round(v, 4) for k, v in out.items()}


def _num(x) -> float:  # noqa: ANN001
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def spend_breakdown(*, db_path: Optional[Path] = None,
                    since: Optional[str] = None) -> dict:
    """Complete factory spend, split by provider / model / episode, plus a
    grand total — from the same two-ledger logic as episode_billed_spend():
    run_authorization_grant_consumption is authoritative for every episode
    that has grant rows (all current episodes; MUAPI video included), and
    the costs table is added only for older episodes with no grant rows, so
    the shared LLM+audio rows are never double-counted.

    `since` (ISO ts) filters both ledgers by their own time column. Returns
    {"total", "by_provider": {p: usd}, "by_model": {m: usd},
     "by_episode": {ep: usd}, "basis"}. Never raises."""
    by_provider: dict[str, float] = {}
    by_model: dict[str, float] = {}
    by_episode: dict[str, float] = {}
    grant_eps: set[str] = set()
    try:
        from hypogeum.db import get_connection
        conn = get_connection(Path(db_path) if db_path else None)
        try:
            state_ph = ",".join("?" for _ in _NON_SPEND_STATES)
            gq = (f"SELECT idempotency_key, provider, model, charged_amount, "
                  f"reserved_amount FROM run_authorization_grant_consumption "
                  f"WHERE idempotency_key LIKE 'ep\\_%' ESCAPE '\\' "
                  f"AND COALESCE(state,'') NOT IN ({state_ph})")
            gp = list(_NON_SPEND_STATES)
            if since:
                gq += " AND created_at >= ?"
                gp.append(since)
            try:
                grant_rows = conn.execute(gq, gp).fetchall()
            except Exception:  # noqa: BLE001 - table absent in a bare schema
                grant_rows = []
            for r in grant_rows:
                ep = str(r["idempotency_key"]).split(":", 1)[0]
                amt = _num(r["charged_amount"]) or _num(r["reserved_amount"])
                amt = max(amt, 0.0)
                grant_eps.add(ep)
                by_episode[ep] = by_episode.get(ep, 0.0) + amt
                if r["provider"]:
                    by_provider[r["provider"]] = by_provider.get(r["provider"], 0.0) + amt
                if r["model"]:
                    by_model[r["model"]] = by_model.get(r["model"], 0.0) + amt

            cq = ("SELECT episode_id, provider, model, cost_usd FROM costs "
                  "WHERE cost_usd IS NOT NULL AND cost_usd > 0")
            cp: list = []
            if since:
                cq += " AND called_at >= ?"
                cp.append(since)
            for r in conn.execute(cq, cp):
                ep = r["episode_id"]
                if ep and ep in grant_eps:
                    continue  # grant ledger already covers this episode
                amt = max(_num(r["cost_usd"]), 0.0)
                if ep:
                    by_episode[ep] = by_episode.get(ep, 0.0) + amt
                if r["provider"]:
                    by_provider[r["provider"]] = by_provider.get(r["provider"], 0.0) + amt
                if r["model"]:
                    by_model[r["model"]] = by_model.get(r["model"], 0.0) + amt
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warn("spend_breakdown_failed", error=str(exc))

    rnd = lambda d: {k: round(v, 4) for k, v in sorted(d.items(), key=lambda kv: -kv[1])}
    return {
        "total": round(sum(by_episode.values()), 4),
        "by_provider": rnd(by_provider),
        "by_model": rnd(by_model),
        "by_episode": rnd(by_episode),
        "basis": "billed",
    }


def spend_since(since: str, *, db_path: Optional[Path] = None) -> float:
    """Total billed spend since an ISO timestamp. Never raises."""
    return spend_breakdown(db_path=db_path, since=since).get("total", 0.0)


def effective_price(model_id: str, *, db_path: Optional[Path] = None) -> Optional[float]:
    """The representative per-call charge for `model_id`, or None if it has
    never been billed. Never raises."""
    if not model_id:
        return None
    s = effective_prices(db_path=db_path).get(model_id)
    return s.effective_price if s else None


def price_view(model_id: str, listed: Optional[float], *,
               db_path: Optional[Path] = None,
               spend: Optional[ModelSpend] = None) -> dict:
    """Template-friendly price summary for one model:
        {listed, effective, n_calls, min, max, drift, drift_pct, flag, label}
    `flag` is True when effective is materially above listed (>25%).
    `label` is the string to show as the headline price.
    Pass `spend` to avoid a second DB hit when iterating a catalogue.
    """
    if spend is None:
        spend = effective_prices(db_path=db_path).get(model_id)
    eff = spend.effective_price if spend else None
    n = spend.n_calls if spend else 0

    drift = drift_pct = None
    flag = False
    if eff is not None and listed and listed > 0:
        drift = round(eff - listed, 4)
        drift_pct = round((eff - listed) / listed * 100, 1)
        flag = (eff - listed) / listed > _DRIFT_THRESHOLD

    if eff is not None and n > 0:
        label = f"${eff:.2f}"
        if spend and spend.min_charge != spend.max_charge:
            label = f"from ${spend.min_charge:.2f}"
    elif listed:
        label = f"${listed:.2f}"
    else:
        label = "—"

    return {
        "listed": listed,
        "effective": eff,
        "n_calls": n,
        "min": spend.min_charge if spend else None,
        "max": spend.max_charge if spend else None,
        "last_charged_at": spend.last_charged_at if spend else None,
        "drift": drift,
        "drift_pct": drift_pct,
        "flag": flag,
        "label": label,
        "basis": "billed" if (eff is not None and n > 0) else "listed",
    }
