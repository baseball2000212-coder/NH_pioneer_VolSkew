"""1주차 · NH선물 REST API 첫 호출

하는 일
  1. ~/NHFutures/config/config.yaml 에서 APP Key·Secret 을 읽는다 (저장소 밖 파일)
  2. 접근토큰을 발급받는다           POST /auth-service/v1/token  (form 형식)
  3. 국내 현재가를 한 번 조회한다      POST /trade/v1/domestic/price (JSON)
  4. 응답을 week01/results/ 에 저장한다 (토큰·키는 저장하지 않음)

실행
  pip install -r requirements.txt
  python week01/first_call.py                 # 모의환경, 코스피200 F 202612
  python week01/first_call.py --sym A016C000  # 종목 지정
  python week01/first_call.py --env real      # 실전 시세 조회(주문 없음)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import requests
import yaml

CONFIG_PATH = Path.home() / "NHFutures" / "config" / "config.yaml"
DEFAULT_BASE_URL = {
    "paper": "https://apidemo.futures.co.kr",
    "real": "https://api.futures.co.kr",
}
TOKEN_PATH = "/auth-service/v1/token"
PRICE_PATH = "/trade/v1/domestic/price"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

# 응답에서 보고서에 옮길 항목
PRICE_FIELDS = [
    "sym_nm", "last_pric", "chg_sign", "chg", "rt",
    "ask_pric", "bid_pric", "open_pric", "high_pric", "low_pric",
    "mltplr", "rem_dy",
]


def mask(value: str) -> str:
    """키·토큰을 그대로 찍지 않도록 앞뒤 4자만 보인다."""
    if not value:
        return "(비어 있음)"
    return f"{value[:4]}...{value[-4:]} ({len(value)}자)" if len(value) > 8 else "*" * len(value)


def load_keys(env: str) -> tuple[str, str, str]:
    if not CONFIG_PATH.exists():
        sys.exit(f"설정 파일이 없습니다: {CONFIG_PATH}\n"
                 f"저장소의 config.example.yaml 을 이 경로로 복사하고 키를 넣으세요.")
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    section = cfg.get(env) or {}
    domestic = section.get("domestic") or {}
    app_key = str(domestic.get("app_key") or "").strip()
    app_secret = str(domestic.get("app_secret") or "").strip()
    if not app_key or not app_secret:
        sys.exit(f"[{env} > domestic] app_key / app_secret 이 비어 있습니다.")
    base_url = str(section.get("base_url") or "").strip() or DEFAULT_BASE_URL[env]
    return base_url.rstrip("/"), app_key, app_secret


def issue_token(base_url: str, app_key: str, app_secret: str) -> dict:
    # 토큰 발급만 form 형식으로 받는다 (JSON 으로 보내면 "필수 파라미터 누락")
    res = requests.post(
        base_url + TOKEN_PATH,
        data={"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret},
        timeout=10,
    )
    if res.status_code >= 400:
        sys.exit(f"토큰 발급 실패 (HTTP {res.status_code})\n{res.text.strip()}")
    body = res.json()
    if not body.get("access_token"):
        sys.exit(f"응답에 access_token 이 없습니다: {list(body.keys())}")
    return body


def domestic_price(base_url: str, token: dict, sym: str) -> tuple[int, dict, float]:
    auth = f"{token.get('token_type') or 'Bearer'} {token['access_token']}"
    started = datetime.now()
    res = requests.post(
        base_url + PRICE_PATH,
        headers={"Authorization": auth, "Content-Type": "application/json"},
        json={"sym": sym},
        timeout=10,
    )
    elapsed_ms = (datetime.now() - started).total_seconds() * 1000
    try:
        body = res.json()
    except ValueError:
        body = {"raw_text": res.text.strip()}
    return res.status_code, body, elapsed_ms


def main() -> None:
    parser = argparse.ArgumentParser(description="NH선물 REST API 첫 호출")
    parser.add_argument("--env", choices=["paper", "real"], default="paper")
    parser.add_argument("--sym", default="A016C000", help="종목 표준코드 (기본: 코스피200 F 202612)")
    args = parser.parse_args()

    base_url, app_key, app_secret = load_keys(args.env)
    print(f"환경     : {args.env}  ({base_url})")
    print(f"APP Key  : {mask(app_key)}")

    token = issue_token(base_url, app_key, app_secret)
    print(f"토큰     : {mask(token['access_token'])}  만료 {token.get('expires_at') or token.get('expires_in')}")

    called_at = datetime.now()
    status, body, elapsed_ms = domestic_price(base_url, token, args.sym)
    print(f"현재가   : HTTP {status}, {elapsed_ms:.0f} ms")

    summary = {k: body.get(k) for k in PRICE_FIELDS if k in body}
    for k, v in summary.items():
        print(f"  {k:<10}: {v}")
    if body.get("msg_cd") or body.get("msg_cntn"):
        print(f"  msg      : {body.get('msg_cd')} {body.get('msg_cntn')}")

    # 근거 파일 — 토큰·키는 넣지 않는다
    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"first_call_{called_at:%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps({
        "called_at": called_at.isoformat(timespec="seconds"),
        "environment": args.env,
        "base_url": base_url,
        "request": {"method": "POST", "path": PRICE_PATH, "body": {"sym": args.sym}},
        "http_status": status,
        "elapsed_ms": round(elapsed_ms),
        "response": body,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장: {out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out}")


if __name__ == "__main__":
    main()
