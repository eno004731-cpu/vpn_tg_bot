from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

WEBHOOK_REQUESTS_TOTAL = Counter(
    "vpn_bot_webhook_requests_total",
    "Total number of webhook requests handled by the bot web process.",
    ("result",),
)
WEBHOOK_REJECTIONS_TOTAL = Counter(
    "vpn_bot_webhook_rejections_total",
    "Total number of rejected webhook requests grouped by reason.",
    ("reason",),
)
WEBHOOK_REQUEST_DURATION_SECONDS = Histogram(
    "vpn_bot_webhook_request_duration_seconds",
    "Webhook request processing time in seconds.",
    ("result",),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
READINESS_FAILURES_TOTAL = Counter(
    "vpn_bot_readiness_failures_total",
    "Total number of readiness check failures caused by dependencies.",
)
JOB_PENDING_GAUGE = Gauge(
    "vpn_bot_jobs_pending",
    "Current number of pending provisioning/notification jobs.",
)
JOB_RUNNING_GAUGE = Gauge(
    "vpn_bot_jobs_running",
    "Current number of running provisioning/notification jobs.",
)
JOB_FAILED_GAUGE = Gauge(
    "vpn_bot_jobs_failed",
    "Current number of failed provisioning/notification jobs.",
)
JOB_ATTEMPTS_TOTAL = Counter(
    "vpn_bot_job_attempts_total",
    "Total number of claimed jobs grouped by type.",
    ("type",),
)
PROVISION_ATTEMPTS_TOTAL = Counter(
    "vpn_bot_provision_attempts_total",
    "Total number of provisioning attempts.",
)
PROVISION_FAILURES_TOTAL = Counter(
    "vpn_bot_provision_failures_total",
    "Total number of failed provisioning attempts.",
)
PROVISION_DURATION_SECONDS = Histogram(
    "vpn_bot_provision_duration_seconds",
    "Provisioning duration in seconds.",
    ("result",),
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)
TELEGRAM_SEND_FAILURES_TOTAL = Counter(
    "vpn_bot_telegram_send_failures_total",
    "Total number of Telegram send failures while delivering access links or alerts.",
)
TRAFFIC_SYNC_FAILURES_TOTAL = Counter(
    "vpn_bot_traffic_sync_failures_total",
    "Total number of traffic sync failures in the worker process.",
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


def observe_webhook_request(result: str, duration_seconds: float) -> None:
    WEBHOOK_REQUESTS_TOTAL.labels(result=result).inc()
    WEBHOOK_REQUEST_DURATION_SECONDS.labels(result=result).observe(duration_seconds)


def observe_webhook_rejection(reason: str) -> None:
    WEBHOOK_REJECTIONS_TOTAL.labels(reason=reason).inc()


def observe_readiness_failure() -> None:
    READINESS_FAILURES_TOTAL.inc()


def observe_job_snapshot(*, pending: int, running: int, failed: int) -> None:
    JOB_PENDING_GAUGE.set(pending)
    JOB_RUNNING_GAUGE.set(running)
    JOB_FAILED_GAUGE.set(failed)


def observe_job_attempt(job_type: str) -> None:
    JOB_ATTEMPTS_TOTAL.labels(type=job_type).inc()


def observe_provision_attempt(duration_seconds: float, *, success: bool) -> None:
    PROVISION_ATTEMPTS_TOTAL.inc()
    PROVISION_DURATION_SECONDS.labels(result="success" if success else "failure").observe(duration_seconds)
    if not success:
        PROVISION_FAILURES_TOTAL.inc()


def observe_telegram_send_failure() -> None:
    TELEGRAM_SEND_FAILURES_TOTAL.inc()


def observe_traffic_sync_failure() -> None:
    TRAFFIC_SYNC_FAILURES_TOTAL.inc()
