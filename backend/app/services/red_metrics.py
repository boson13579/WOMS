"""Aggregation service for the RED / SLO observability endpoints.

The :mod:`app.core.red_metrics` middleware writes one sample per request
into ``metrics:requests`` (Redis sorted set). This service is the read
side: it pulls the trailing window, decodes the JSON members, and rolls
the data up into the DTOs that the API surfaces.

Two public callables:

* :func:`compute_window` — RED metrics over a short trailing window
  (10-300 s, default 60 s). Surfaced at ``GET /api/v1/system/red``.
* :func:`compute_slo` — error-budget calculation over a longer window
  (1-168 h, default 24 h). Surfaced at ``GET /api/v1/system/slo``.

Both functions are pure CPU work over a Redis read — no DB, no I/O
besides the single ``ZRANGEBYSCORE``. Empty windows return zero
envelopes (not 404) so the frontend can render "0 req/s" instead of an
error state.

Retention note (documented in the SLO endpoint docstring): the ZSET is
trimmed to the last 1 hour by the middleware (see
``app/core/red_metrics.py:RETENTION_MS``). A 24h SLO window only sees
the samples physically present, so ``total_requests`` represents that
available slice rather than the full 24h truth — the response carries
``data_window_seconds_actual`` so callers can show "Showing Xm of data
(requested Yh)" rather than silently misleading the operator.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any, Literal, cast

import structlog
from redis import Redis

from app.core.config import get_settings
from app.core.red_metrics import METRICS_KEY, _get_metrics_redis
from app.schemas.system import (
    EndpointStat,
    LatencyPercentiles,
    RedMetricsResponse,
    SloComplianceResponse,
)

logger = structlog.get_logger(__name__)

# Top-N endpoints surfaced in the RED ``by_endpoint`` array. The full
# bucket list could be hundreds of routes on a real app — the dashboard
# only renders a handful, so we trim server-side to avoid shipping the
# tail across the wire.
_BY_ENDPOINT_TOP_N = 10

# HTTP status-code thresholds. Named so the comparison sites read like
# specification rather than magic numbers; also keeps ruff PLR2004 happy.
_HTTP_CLIENT_ERROR_MIN = 400
_HTTP_SERVER_ERROR_MIN = 500
_PERCENT_MAX = 100

# Error-classification rules per Plan A1:
#   * All 5xx are errors (server problem).
#   * 4xx is an error IF it's not 401 / 403 / 404 — those represent caller
#     bugs / auth flow, not service errors. Counting them would pollute
#     the error rate every time an unauthenticated client polls.
_EXCLUDED_4XX = frozenset((401, 403, 404))


def _is_error(status_code: int) -> bool:
    """Return True when *status_code* counts as an error for RED purposes.

    See :data:`_EXCLUDED_4XX` for the rationale on which 4xx codes do NOT
    count: 401 / 403 / 404 are normal traffic patterns (auth failures,
    not-found polling) rather than service errors.
    """
    if status_code >= _HTTP_SERVER_ERROR_MIN:
        return True
    return (
        _HTTP_CLIENT_ERROR_MIN <= status_code < _HTTP_SERVER_ERROR_MIN
        and status_code not in _EXCLUDED_4XX
    )


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Return the *pct* percentile of *sorted_values*.

    Uses the nearest-rank method (NIST style): ``rank = ceil(pct / 100 * n)``
    indexed from 1. Returns 0 when the list is empty — callers handle the
    "no samples in window" case by short-circuiting before getting here, so
    in practice this fallback is just defensive.

    Why not :func:`statistics.quantiles`: that needs ``n >= 2``. Our path
    receives zero / one-element lists during early dashboard polls and we
    want the percentiles to silently produce 0 / the single value rather
    than raise.
    """
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    rank = max(1, math.ceil(pct / 100 * n))
    return sorted_values[min(rank - 1, n - 1)]


def _now_ms() -> int:
    """Current epoch in milliseconds — extracted so tests can monkeypatch."""
    return int(time.time() * 1000)


def _read_samples(rds: Redis, *, since_ms: int) -> list[dict[str, Any]]:
    """Pull every sample with ``score >= since_ms`` and decode the JSON.

    Decoding failures are logged + skipped: a corrupted member must not
    take down the endpoint. (In practice the middleware writes well-formed
    JSON; this guard exists for forward-compat if the schema ever changes.)
    """
    raw = cast("list[str]", rds.zrangebyscore(METRICS_KEY, since_ms, "+inf"))
    out: list[dict[str, Any]] = []
    for entry in raw:
        try:
            doc = json.loads(entry)
        except json.JSONDecodeError as exc:
            logger.warning("red_metrics.bad_sample", error=str(exc), raw=entry)
            continue
        if isinstance(doc, dict):
            out.append(doc)
    return out


def _endpoint_label(sample: dict[str, Any]) -> str:
    """Return the human-readable endpoint label ``"METHOD PATH"``.

    Samples carry the path in ``p`` and method in ``m`` (compact field
    names — see :func:`app.core.red_metrics._record_sample`). The label
    format matches the dashboard's display: ``"POST /api/v1/orders"``.
    """
    method = str(sample.get("m", "GET"))
    path = str(sample.get("p", "<unknown>"))
    return f"{method} {path}"


def _empty_response(
    window_seconds: int,
    *,
    data_status: Literal["ok", "degraded"] = "ok",
) -> RedMetricsResponse:
    """Return the all-zero RED envelope for an empty window.

    Same shape as the populated response so the frontend never has to
    branch on "no data" — every field is present, just zeroed.

    *data_status* defaults to ``"ok"`` (legitimate "no traffic in
    window") and is set to ``"degraded"`` by callers in the Redis
    exception paths so the frontend can warn that the numbers do not
    reflect live state. Both zero envelopes are otherwise identical so
    the dashboard's empty-state rendering still works.
    """
    return RedMetricsResponse(
        window_seconds=window_seconds,
        total_requests=0,
        rate_per_sec=0.0,
        error_count=0,
        error_pct=0.0,
        latency_ms=LatencyPercentiles(p50=0, p95=0, p99=0, max=0),
        by_endpoint=[],
        data_status=data_status,
    )


def compute_window(window_seconds: int) -> RedMetricsResponse:
    """Aggregate the trailing *window_seconds* of RED samples.

    Pipeline:

    1. ``ZRANGEBYSCORE metrics:requests {now - window_ms} +inf`` — pull
       every sample inside the window.
    2. Decode each JSON member into ``{p, m, s, d, t}`` fields.
    3. Roll up totals (count, error count, latency list).
    4. Group by endpoint label and compute per-endpoint p50 / p95 / p99
       + error percent.
    5. Sort the per-endpoint slice by count DESC, slice to top 10.

    *window_seconds* is trusted (the endpoint validates ``ge=1`` via
    Query). Windows wider than the underlying 5-min retention are
    silently capped by data availability — we return whatever is there
    rather than erroring (per A1 plan).
    """
    try:
        rds = _get_metrics_redis()
    except Exception as exc:
        logger.warning("red_metrics.redis_unavailable", error=str(exc))
        return _empty_response(window_seconds, data_status="degraded")

    now_ms = _now_ms()
    since_ms = now_ms - window_seconds * 1000

    try:
        samples = _read_samples(rds, since_ms=since_ms)
    except Exception as exc:
        logger.warning("red_metrics.read_failed", error=str(exc))
        return _empty_response(window_seconds, data_status="degraded")

    if not samples:
        return _empty_response(window_seconds)

    total = len(samples)
    error_count = sum(1 for s in samples if _is_error(int(s.get("s", 0))))
    durations = sorted(float(s.get("d", 0.0)) for s in samples)

    p50 = _percentile(durations, 50)
    p95 = _percentile(durations, 95)
    p99 = _percentile(durations, 99)
    duration_max = durations[-1] if durations else 0.0

    # Group samples by endpoint label so the by_endpoint slice can carry
    # per-route percentiles. The ``buckets`` dict is keyed by the same
    # label string the frontend renders, so order is stable: insertion
    # order is sample order, then sorted by count for the top-N output.
    buckets: dict[str, list[dict[str, Any]]] = {}
    for s in samples:
        buckets.setdefault(_endpoint_label(s), []).append(s)

    by_endpoint: list[EndpointStat] = []
    for label, bucket in buckets.items():
        bucket_durations = sorted(float(s.get("d", 0.0)) for s in bucket)
        bucket_errors = sum(1 for s in bucket if _is_error(int(s.get("s", 0))))
        count = len(bucket)
        by_endpoint.append(
            EndpointStat(
                endpoint=label,
                count=count,
                error_pct=round(bucket_errors / count * 100, 2) if count else 0.0,
                p50_ms=round(_percentile(bucket_durations, 50)),
                p95_ms=round(_percentile(bucket_durations, 95)),
                p99_ms=round(_percentile(bucket_durations, 99)),
            )
        )

    by_endpoint.sort(key=lambda e: e.count, reverse=True)
    by_endpoint = by_endpoint[:_BY_ENDPOINT_TOP_N]

    return RedMetricsResponse(
        window_seconds=window_seconds,
        total_requests=total,
        rate_per_sec=round(total / window_seconds, 2) if window_seconds > 0 else 0.0,
        error_count=error_count,
        error_pct=round(error_count / total * 100, 2) if total else 0.0,
        latency_ms=LatencyPercentiles(
            p50=round(p50),
            p95=round(p95),
            p99=round(p99),
            max=round(duration_max),
        ),
        by_endpoint=by_endpoint,
    )


def compute_slo(window_hours: int) -> SloComplianceResponse:
    """Compute SLO compliance + error-budget remaining over *window_hours*.

    Reuses the same ``metrics:requests`` ZSET as :func:`compute_window`
    (just with a longer window). Successful = HTTP status < 500; we count
    only server-side errors here because the SLO target represents the
    availability of OUR service, not the correctness of every client.

    Budget math:

        success_pct           = successful / total * 100
        error_budget_consumed = (100 - success_pct) / (100 - slo_target) * 100
        budget_remaining       = 100 - error_budget_consumed
        # both clamped to [0, 100] in case the actual error pct exceeds
        # the planned-for budget (over-budget is reported as 0%).

    Also returns ``data_window_seconds_actual``: how many seconds of data
    are actually backing the response. Computed as
    ``min(requested_window_seconds, now - oldest_sample_ts)``. Lets the
    frontend show "Showing 1h of data (requested 24h)" when the requested
    window exceeds the underlying ZSET retention.

    Empty window (``total_requests == 0``) → ``success_pct = 100`` and
    full budget remaining. The plan documents this choice: no traffic
    means no budget spent, so we report a healthy SLO rather than
    null / 0 (which would scare on-call for no reason).
    """
    settings = get_settings()
    slo_target_pct = float(settings.SLO_AVAILABILITY_TARGET_PCT)
    requested_window_seconds = window_hours * 3600

    try:
        rds = _get_metrics_redis()
    except Exception as exc:
        logger.warning("red_metrics.redis_unavailable", error=str(exc))
        return _empty_slo_response(window_hours, slo_target_pct, data_status="degraded")

    now_ms = _now_ms()
    since_ms = now_ms - requested_window_seconds * 1000

    try:
        samples = _read_samples(rds, since_ms=since_ms)
    except Exception as exc:
        logger.warning("red_metrics.read_failed", error=str(exc))
        return _empty_slo_response(window_hours, slo_target_pct, data_status="degraded")

    total = len(samples)
    if total == 0:
        return _empty_slo_response(window_hours, slo_target_pct)

    # "Successful" for SLO purposes means "service did its job" = status < 500.
    # 4xx are caller-side problems and don't count against availability.
    successful = sum(1 for s in samples if int(s.get("s", 0)) < _HTTP_SERVER_ERROR_MIN)
    success_pct = successful / total * _PERCENT_MAX

    # Headroom available between the SLO bar (e.g. 99.5%) and 100%. When
    # the SLO target is exactly 100% (denominator zero) we conservatively
    # report no budget remaining — that case is forbidden by the
    # ``lt=100`` Field constraint but we guard anyway.
    headroom = _PERCENT_MAX - slo_target_pct
    if headroom <= 0:
        consumed = float(_PERCENT_MAX) if success_pct < _PERCENT_MAX else 0.0
    else:
        consumed = (_PERCENT_MAX - success_pct) / headroom * _PERCENT_MAX

    # Clamp to [0, 100]: above-target traffic should not produce a
    # negative consumed-pct, and over-budget traffic caps at "all gone".
    consumed = max(0.0, min(float(_PERCENT_MAX), consumed))
    remaining = max(0.0, min(float(_PERCENT_MAX), float(_PERCENT_MAX) - consumed))

    # Surface the actual data window: oldest sample timestamp vs now,
    # capped at the requested window. Lets the frontend show "Showing
    # Xm of data (requested Yh)" when ZSET retention < requested window.
    oldest_ts_ms = min(int(s.get("t", now_ms)) for s in samples)
    actual_window_seconds = min(requested_window_seconds, max(0, (now_ms - oldest_ts_ms) // 1000))

    return SloComplianceResponse(
        window_hours=window_hours,
        total_requests=total,
        successful_requests=successful,
        success_pct=round(success_pct, 2),
        slo_target_pct=round(slo_target_pct, 2),
        error_budget_pct_remaining=round(remaining, 2),
        error_budget_consumed_pct=round(consumed, 2),
        data_window_seconds_actual=int(actual_window_seconds),
    )


def _empty_slo_response(
    window_hours: int,
    slo_target_pct: float,
    *,
    data_status: Literal["ok", "degraded"] = "ok",
) -> SloComplianceResponse:
    """Return the all-zero / full-budget SLO envelope for an empty window.

    Per the plan: no traffic → 100% success, 100% budget remaining.
    Treating "no traffic" as "healthy" matches operator intuition (and
    avoids waking up the on-call when no users are hitting the system).

    ``data_window_seconds_actual`` is 0 because no samples means no
    data window at all — the frontend can render the card without a
    "data: last Xm" hint in this case.

    *data_status* defaults to ``"ok"`` (legitimate empty window). The
    Redis exception paths in :func:`compute_slo` pass ``"degraded"`` so
    the frontend can warn that the headline "100% available" is actually
    "we have no data to evaluate" — surfacing a healthy bar during a
    metrics-source outage would mislead the operator.
    """
    return SloComplianceResponse(
        window_hours=window_hours,
        total_requests=0,
        successful_requests=0,
        success_pct=100.0,
        slo_target_pct=round(slo_target_pct, 2),
        error_budget_pct_remaining=100.0,
        error_budget_consumed_pct=0.0,
        data_window_seconds_actual=0,
        data_status=data_status,
    )


__all__ = ["compute_slo", "compute_window"]
