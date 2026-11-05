"""호출 제한을 지키기 위해 요청 사이를 벌립니다.

초당 건수를 최소 간격으로 바꿔(초당 10건이면 0.1초) 요청이 그보다 촘촘히 나가지 않게 합니다.
몰아 보내고 쉬는 방식보다 간격을 지키는 쪽이 안전합니다.

세는 단위는 **(환경, 시장, 등급)** 입니다. 국내와 해외는 APP Key 가 따로 발급되는 서로
다른 계좌라 서로의 몫을 깎지 않습니다.

기다리는 동안 스레드가 멈추므로 이벤트 루프 안에서는 `asyncio.to_thread` 로 부르세요.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .catalog import LIMIT_PER_SECOND, Limit


class RateLimiter:
    """등급별로 요청 간격을 지킵니다."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # 시계와 대기를 바꿔 끼울 수 있게 받아 둡니다. 테스트에서 실제로 기다리지 않습니다.
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_allowed: dict[tuple[str, str, Limit], float] = {}

    @staticmethod
    def interval(limit: Limit) -> float:
        """이 등급에서 요청 사이에 두어야 할 최소 간격(초)."""
        return 1.0 / LIMIT_PER_SECOND[limit]

    def wait(self, environment: str, market: str, limit: Limit) -> float:
        """다음 요청을 보내도 될 때까지 기다리고, 기다린 시간(초)을 돌려줍니다."""
        interval = self.interval(limit)
        key = (environment, market, limit)
        with self._lock:
            now = self._monotonic()
            allowed_at = self._next_allowed.get(key, now)
            delay = max(0.0, allowed_at - now)
            self._next_allowed[key] = max(allowed_at, now) + interval
        if delay > 0:
            self._sleep(delay)
        return delay

    def reset(self) -> None:
        """지금까지의 간격 기록을 버립니다."""
        with self._lock:
            self._next_allowed.clear()
