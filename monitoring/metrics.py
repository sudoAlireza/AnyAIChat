import time
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


class Metrics:
    """Simple in-process metrics collection with periodic logging."""

    def __init__(self):
        self._counters = defaultdict(int)
        self._gauges = defaultdict(float)
        self._latencies = defaultdict(list)
        self._start_time = time.monotonic()

    def increment(self, name: str, value: int = 1):
        self._counters[name] += value

    def set_gauge(self, name: str, value: float):
        self._gauges[name] = value

    def record_latency(self, name: str, seconds: float):
        self._latencies[name].append(seconds)
        # Keep only last 1000 entries per metric
        if len(self._latencies[name]) > 1000:
            self._latencies[name] = self._latencies[name][-500:]

    def get_summary(self) -> dict:
        summary = {
            "uptime_seconds": round(time.monotonic() - self._start_time, 1),
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
            "latencies": {},
        }
        for name, values in self._latencies.items():
            if values:
                sorted_vals = sorted(values)
                summary["latencies"][name] = {
                    "count": len(values),
                    "avg": round(sum(values) / len(values), 3),
                    "p50": round(sorted_vals[len(sorted_vals) // 2], 3),
                    "p95": round(sorted_vals[int(len(sorted_vals) * 0.95)], 3),
                    "max": round(max(values), 3),
                }
        return summary

    def log_summary(self):
        """Log current metrics summary."""
        summary = self.get_summary()
        logger.info(
            f"Metrics | uptime={summary['uptime_seconds']}s | "
            f"counters={summary['counters']} | "
            f"gauges={summary['gauges']} | "
            f"latencies={summary['latencies']}"
        )

    def reset_latencies(self):
        """Reset latency data after logging."""
        self._latencies.clear()


# Global metrics instance
metrics = Metrics()


async def log_metrics_task():
    """APScheduler-compatible async task for periodic metrics logging."""
    metrics.log_summary()
