"""실시간(WebSocket) 시세·주문체결 수신.

참고: 운영진 예시 앱 `backend/realtime/link.py`, `feeds.py` 에서 확인된 사실을 따라 새로 썼습니다.

**접속 절차**
1. 시장별 접속 토큰으로 웹소켓 접속키를 발급받습니다. 키는 **1회용**이라 연결(재접속 포함)마다 새로 받습니다.
2. `wss://{호스트}/trade/ws-stream?access_key=<키>` 로 접속합니다. 주소에 키가 들어가므로 **주소를 기록에 남기지 않습니다.**
3. `session_init`(본문에 접속 토큰)을 보내고 `INIT_SUCCESS` 를 기다립니다.
4. 구독은 `action: "A"`, 해지는 `"D"`. 서버는 `RT_INIT`(구독 확인) 또는 `RT_FAIL` 로 답합니다.

**세션 유지**: 서버가 `{"code":"pingpong"}` 을 보내면 **받은 그대로 되돌려야** 합니다. 웹소켓 규격의
핑 프레임은 끕니다(켜면 국내 연결이 주기적으로 끊깁니다). 서버가 한동안 아무것도 안 보내면 죽은 연결로 보고 다시 붙습니다.

**재접속**: 최근 10분 안에 짧게 끊긴 횟수만큼 1·2·5·10·30·60초로 벌려 다시 붙고, 붙으면 구독을 **전부 다시** 겁니다.
**끊긴 동안의 값은 메워지지 않습니다.** 다시 붙으면 `on_state("refill_needed")` 로 알리니 REST 조회로 채우세요.

**구독 한도**: APP Key 하나에 20건. 국내 체결·호가는 주간·야간 짝(KA↔KC, KB↔KD)이 서버에서 한 등록으로
묶입니다(한쪽을 해지하면 짝도 끊김). 짝이 한도에 한 건인지 두 건인지는 문서에 없어 **두 건으로 세어 안전하게** 막습니다.

국내 KA/KC 는 `tick_div` 가 1 인 것만 체결입니다(0 은 체결량 0 인 기타 데이터). `exec_div` 의 매수·매도 뜻은
명세에 없어 단정하지 않습니다.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from .auth import TokenManager
from .catalog import STREAM_APIS, STREAM_PATH, STREAM_SUBSCRIPTION_LIMIT, Limit, stream_spec
from .config import PAPER, Config
from .errors import AuthError, NHApiError
from .journal import Journal
from .ratelimit import RateLimiter

API_VERSION = "1"
ACTION_INIT = "0"
ACTION_SUBSCRIBE = "A"
ACTION_UNSUBSCRIBE = "D"
INIT_ACTION_CODE = "session_init"
INIT_CODE = "INIT"
INIT_SUCCESS = "INIT_SUCCESS"
PINGPONG_CODE = "pingpong"
SUBSCRIBED_CODE = "RT_INIT"
FAILED_CODE = "RT_FAIL"

OPEN_TIMEOUT = 10.0
INIT_TIMEOUT = 10.0
CLOSE_TIMEOUT = 1.0
#: 서버가 이만큼 아무것도 안 보내면 죽은 연결로 봅니다.
IDLE_TIMEOUT = 90.0
#: 다시 붙기까지 기다리는 시간(초). 최근에 짧게 끊긴 횟수 n 이면 n 번째 값.
BACKOFF = (0.0, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
#: 이만큼은 붙어 있어야 「제대로 붙었다」로 봅니다.
SETTLED_AFTER = 60.0
UNSETTLED_WINDOW = 600.0

CONNECTING = "connecting"
LIVE = "live"
RECONNECTING = "reconnecting"
STOPPED = "stopped"
REFILL_NEEDED = "refill_needed"

_PAIRS = {"KA": "KC", "KC": "KA", "KB": "KD", "KD": "KB"}
_ORDER_FEEDS = ("O1_K", "O1")


class SubscriptionLimitError(NHApiError):
    """구독 한도(20건)를 넘기는 요청."""

    def __init__(self, message: str) -> None:
        super().__init__("ws", message)


@dataclass(frozen=True)
class Feed:
    """구독 하나. `api_id` 는 KA/KB/KC/KD/O1_K/FA/FB/O1, 주문/체결 알림은 계좌 단위라 `sym` 이 비어 있습니다."""

    api_id: str
    sym: str = ""

    def __post_init__(self) -> None:
        stream_spec(self.api_id)  # 모르는 이름이면 ValueError
        if self.api_id in _ORDER_FEEDS and self.sym:
            raise ValueError("주문/체결 알림은 종목 없이 구독합니다.")
        if self.api_id not in _ORDER_FEEDS and not self.sym:
            raise ValueError(f"{self.api_id} 는 종목코드가 필요합니다.")

    @property
    def market(self) -> str:
        return stream_spec(self.api_id).market

    @property
    def action_code(self) -> str:
        return stream_spec(self.api_id).action_code


def paired(feed: Feed) -> Feed | None:
    """주간·야간 짝. 국내 체결·호가만 있습니다."""
    other = _PAIRS.get(feed.api_id)
    return Feed(other, feed.sym) if other else None


def api_id_of(market: str, action_code: str) -> str | None:
    """수신 메시지의 `action_code` 가 어느 실시간 API 인지. 주문/체결은 계좌(시장)가 갈라 줍니다."""
    for spec in STREAM_APIS.values():
        if spec.market == market and spec.action_code == action_code:
            return spec.api_id
    return None


def subscribe_message(feed: Feed, *, action: str) -> dict[str, Any]:
    body: dict[str, Any] = {"action_code": feed.action_code}
    if feed.sym:
        body["action_sym"] = feed.sym
    return {"header": {"api_ver": API_VERSION, "action": action}, "body": body}


def init_message(session_token: str) -> dict[str, Any]:
    return {
        "header": {"api_ver": API_VERSION, "action": ACTION_INIT},
        "body": {"action_code": INIT_ACTION_CODE, "session_token": session_token},
    }


def is_trade_tick(api_id: str, body: dict[str, Any]) -> bool:
    """체결 메시지인지. 국내 KA/KC 는 `tick_div` 가 1 이고 체결량이 0 보다 커야 합니다."""
    if api_id not in ("KA", "KC"):
        return True
    try:
        quantity = Decimal(str(body.get("exec_qty") or "0"))
    except Exception:  # noqa: BLE001
        return False
    return str(body.get("tick_div") or "") == "1" and quantity > 0


class SubscriptionLedger:
    """걸어 달라는 구독과 서버에 실제로 등록할 구독(짝 포함)의 장부."""

    def __init__(self, limit: int = STREAM_SUBSCRIPTION_LIMIT) -> None:
        self.limit = limit
        self._feeds: dict[Feed, None] = {}

    def registrations(self) -> list[Feed]:
        made: dict[Feed, None] = dict(self._feeds)
        for feed in self._feeds:
            partner = paired(feed)
            if partner is not None:
                made.setdefault(partner, None)
        return list(made)

    def add(self, feed: Feed) -> list[Feed]:
        """구독을 추가하고 **새로 등록해야 할** 것을 돌려줍니다. 한도를 넘기면 SubscriptionLimitError."""
        if feed in self._feeds:
            return []
        before = set(self.registrations())
        self._feeds[feed] = None
        after = self.registrations()
        if len(after) > self.limit:
            del self._feeds[feed]
            raise SubscriptionLimitError(f"구독 한도 {self.limit}건을 넘깁니다 (짝 포함 {len(after)}건).")
        return [item for item in after if item not in before]

    def remove(self, feed: Feed) -> list[Feed]:
        """구독을 빼고 **서버에서 해지할** 것을 돌려줍니다. 짝이 아직 필요하면 해지하지 않습니다."""
        if feed not in self._feeds:
            return []
        before = set(self.registrations())
        del self._feeds[feed]
        after = set(self.registrations())
        return [item for item in before if item not in after]

    def __len__(self) -> int:
        return len(self._feeds)


class Pacer:
    """끊긴 뒤 다시 붙기까지 얼마나 기다릴지 정합니다."""

    def __init__(
        self,
        backoff: tuple[float, ...] = BACKOFF,
        *,
        settled_after: float = SETTLED_AFTER,
        window: float = UNSETTLED_WINDOW,
    ) -> None:
        self._backoff = backoff
        self._settled_after = settled_after
        self._window = window
        self._unsettled: list[float] = []

    def after(self, *, now: float, lasted: float, failed: bool) -> float:
        self._unsettled = [at for at in self._unsettled if now - at < self._window]
        if failed or lasted < self._settled_after:
            self._unsettled.append(now)
        return self._backoff[min(len(self._unsettled), len(self._backoff) - 1)]


def ws_base(base_url: str) -> str:
    """`https://` 주소를 `wss://` 로."""
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://"):]
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://"):]
    return base_url


OnMessage = Callable[[str, str, dict[str, Any]], None]
OnState = Callable[[str, str], None]


class StreamClient:
    """시장 하나의 실시간 연결. 붙어 있는 동안 구독을 지키고 끊기면 다시 붙습니다."""

    def __init__(
        self,
        config: Config,
        tokens: TokenManager,
        market: str,
        *,
        on_message: OnMessage,
        on_state: OnState | None = None,
        journal: Journal | None = None,
        connect: Any = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        idle_timeout: float = IDLE_TIMEOUT,
        backoff: tuple[float, ...] = BACKOFF,
        settled_after: float = SETTLED_AFTER,
    ) -> None:
        self._config = config
        self._tokens = tokens
        self.market = market
        self._on_message = on_message
        self._on_state = on_state
        self._journal = journal
        self._connect = connect or ws_connect
        self._limiter = limiter or RateLimiter()
        self._sleep = sleep
        self._clock = clock
        self._idle_timeout = idle_timeout
        self._pacer = Pacer(backoff, settled_after=settled_after)
        self.ledger = SubscriptionLedger()
        self._socket: Any = None
        self._stopping = False
        self._state = STOPPED
        self._ever_live = False
        #: 서버가 거절한 구독 → (코드, 글)
        self.failures: dict[Feed, tuple[str, str]] = {}
        self.last_close: tuple[Any, Any] = (None, None)

    @property
    def state(self) -> str:
        return self._state

    # ------------------------------------------------------------ 구독

    async def subscribe(self, feed: Feed) -> None:
        if feed.market != self.market:
            raise ValueError(f"{feed.api_id} 는 {feed.market} 구독입니다 (이 연결은 {self.market}).")
        for item in self.ledger.add(feed):
            await self._send(subscribe_message(item, action=ACTION_SUBSCRIBE))

    async def unsubscribe(self, feed: Feed) -> None:
        for item in self.ledger.remove(feed):
            self.failures.pop(item, None)
            await self._send(subscribe_message(item, action=ACTION_UNSUBSCRIBE))

    # ------------------------------------------------------------ 실행

    async def run(self) -> None:
        """연결을 유지합니다. `stop()` 이 불리면 끝납니다."""
        self._stopping = False
        wait = 0.0
        while not self._stopping:
            if wait:
                await self._sleep(wait)
            if self._stopping:
                break
            started = self._clock()
            failed = False
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 어떤 실패든 다시 붙습니다.
                failed = True
                self._log_error("session", exc)
            if self._stopping:
                break
            wait = self._pacer.after(now=self._clock(), lasted=self._clock() - started, failed=failed)
            self._set_state(RECONNECTING, f"{wait:g}초 뒤 다시 접속")
        self._set_state(STOPPED, "")

    async def stop(self) -> None:
        """구독을 해지하고 연결을 닫습니다."""
        self._stopping = True
        socket, self._socket = self._socket, None
        if socket is not None:
            for feed in self.ledger.registrations():
                await self._raw_send(socket, subscribe_message(feed, action=ACTION_UNSUBSCRIBE))
            try:
                await asyncio.wait_for(socket.close(), CLOSE_TIMEOUT)
            except Exception:  # noqa: BLE001 — 닫기 응답이 없어도 소켓은 닫힙니다.
                pass

    # ------------------------------------------------------------ 내부

    async def _session(self) -> None:
        self._set_state(CONNECTING, "")
        await asyncio.to_thread(self._limiter.wait, PAPER, self.market, Limit.AUTH)
        access_key = await asyncio.to_thread(self._tokens.websocket_key, self.market)
        token = await asyncio.to_thread(self._tokens.token, self.market)
        url = f"{ws_base(self._config.base_url)}{STREAM_PATH}?access_key={quote(access_key, safe='')}"
        async with self._connect(url, ping_interval=None, open_timeout=OPEN_TIMEOUT, close_timeout=CLOSE_TIMEOUT) as socket:
            self._socket = socket
            try:
                await self._raw_send(socket, init_message(token.access_token))
                await self._await_init(socket)
                self.failures.clear()
                for feed in self.ledger.registrations():
                    await self._raw_send(socket, subscribe_message(feed, action=ACTION_SUBSCRIBE))
                self._set_state(LIVE, "")
                if self._ever_live:
                    self._set_state(REFILL_NEEDED, "끊긴 동안의 값은 메워지지 않았습니다. 조회로 채우세요.")
                self._ever_live = True
                await self._listen(socket)
            finally:
                self._socket = None

    async def _await_init(self, socket: Any) -> None:
        deadline = self._clock() + INIT_TIMEOUT
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise AuthError("ws", "실시간 초기화(INIT_SUCCESS) 응답이 없습니다.")
            raw = await asyncio.wait_for(socket.recv(), max(remaining, 0.001))
            message = _loads(raw)
            if message is None:
                continue
            code = message.get("code")
            if code == PINGPONG_CODE:
                await self._raw_send(socket, raw)
            elif code == INIT_CODE:
                if message.get("msg") == INIT_SUCCESS:
                    return
                raise AuthError("ws", f"실시간 초기화가 거절되었습니다: {message.get('msg')}")

    async def _listen(self, socket: Any) -> None:
        while not self._stopping:
            try:
                raw = await asyncio.wait_for(socket.recv(), self._idle_timeout)
            except asyncio.TimeoutError:
                raise AuthError("ws", f"{self._idle_timeout:g}초 동안 수신이 없어 연결을 다시 맺습니다.") from None
            except ConnectionClosed as exc:
                rcvd = getattr(exc, "rcvd", None)
                self.last_close = (getattr(rcvd, "code", None), getattr(rcvd, "reason", None))
                if self._journal is not None:
                    self._journal.event("ws_closed", "서버가 연결을 닫았습니다", market=self.market,
                                        close_code=self.last_close[0], close_reason=self.last_close[1])
                return
            await self._handle(socket, raw)

    async def _handle(self, socket: Any, raw: Any) -> None:
        message = _loads(raw)
        if message is None:
            return
        code = message.get("code")
        if code == PINGPONG_CODE:
            await self._raw_send(socket, raw)
            return
        if code == SUBSCRIBED_CODE:
            return
        if code == FAILED_CODE or (isinstance(code, str) and code.startswith("EST")):
            data = message.get("data") if isinstance(message.get("data"), dict) else {}
            feed = None
            try:
                api_id = api_id_of(self.market, str(data.get("action_code") or ""))
                if api_id:
                    feed = Feed(api_id, str(data.get("action_sym") or ""))
            except ValueError:
                feed = None
            if feed is not None:
                self.failures[feed] = (str(code), str(message.get("msg") or ""))
            if self._journal is not None:
                self._journal.error("ws_subscribe", feed.sym if feed else None,
                                    code=str(code), msg=str(message.get("msg") or ""), action="skip")
            return
        header = message.get("header")
        body = message.get("body")
        if isinstance(header, dict) and isinstance(body, dict):
            api_id = api_id_of(self.market, str(header.get("action_code") or ""))
            if api_id is not None:
                self._on_message(api_id, str(header.get("action_sym") or ""), body)

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._socket is not None:
            await self._raw_send(self._socket, payload)

    async def _raw_send(self, socket: Any, payload: Any) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        try:
            await socket.send(text)
        except (WebSocketException, OSError, RuntimeError):
            pass  # 보내다 끊겼습니다. 다시 붙을 때 구독을 전부 겁니다.

    def _set_state(self, state: str, detail: str) -> None:
        if state != REFILL_NEEDED:
            if state == self._state:
                return
            self._state = state
        if self._journal is not None:
            self._journal.event("ws_state", detail, market=self.market, state=state)
        if self._on_state is not None:
            self._on_state(state, detail)

    def _log_error(self, where: str, exc: BaseException) -> None:
        if self._journal is not None:
            self._journal.error(
                "ws_session", None, where=where, msg=f"{type(exc).__name__}: {exc}", action="reconnect"
            )


def _loads(raw: Any) -> dict[str, Any] | None:
    try:
        message = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return message if isinstance(message, dict) else None
