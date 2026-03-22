import time
import logging
from collections import defaultdict
from config import RATE_LIMIT_PER_MINUTE, RATE_LIMIT_PER_HOUR

logger = logging.getLogger(__name__)


class RateLimiter:
    """In-memory per-user rate limiter using sliding window."""

    def __init__(self, per_minute=None, per_hour=None):
        self.per_minute = per_minute or RATE_LIMIT_PER_MINUTE
        self.per_hour = per_hour or RATE_LIMIT_PER_HOUR
        self._timestamps = defaultdict(list)

    def _cleanup(self, user_id: int, now: float):
        """Remove timestamps older than 1 hour."""
        cutoff = now - 3600
        self._timestamps[user_id] = [
            ts for ts in self._timestamps[user_id] if ts > cutoff
        ]

    def is_allowed(self, user_id: int) -> bool:
        """Check if a user is within rate limits. Records the request if allowed."""
        now = time.monotonic()
        self._cleanup(user_id, now)

        timestamps = self._timestamps[user_id]

        # Check per-minute limit
        one_minute_ago = now - 60
        recent_minute = sum(1 for ts in timestamps if ts > one_minute_ago)
        if recent_minute >= self.per_minute:
            logger.warning(f"Rate limit (per-minute) exceeded for user {user_id}")
            return False

        # Check per-hour limit
        if len(timestamps) >= self.per_hour:
            logger.warning(f"Rate limit (per-hour) exceeded for user {user_id}")
            return False

        timestamps.append(now)
        return True

    def get_wait_time(self, user_id: int) -> float:
        """Return seconds until the user can send again (0 if allowed now)."""
        now = time.monotonic()
        self._cleanup(user_id, now)
        timestamps = self._timestamps[user_id]

        if not timestamps:
            return 0.0

        one_minute_ago = now - 60
        recent_minute = [ts for ts in timestamps if ts > one_minute_ago]

        if len(recent_minute) >= self.per_minute:
            return max(0.0, recent_minute[0] - one_minute_ago)

        if len(timestamps) >= self.per_hour:
            return max(0.0, timestamps[0] - (now - 3600))

        return 0.0


# Global rate limiter instance
rate_limiter = RateLimiter()
