# 1주차 · API 키 발급과 첫 호출

**제출일: 2026/10/23(금) 23:59**

## 체크리스트

- [x] 계좌로 APP Key·Secret Key 발급 (모의 · 국내)
- [x] 발급받은 키로 접근토큰을 받고 API 한 번 호출
- [x] 팀 GitHub 저장소 생성
- [x] 저장소 README 에 팀명·소개·주차 표 작성
- [x] 운영진 저장소 접근 확인
- [x] 팀 Discord 방에 저장소 링크 제출

## 첫 호출: 국내 현재가

| 단계 | API | 방식 |
|------|-----|------|
| 1. 접근토큰 발급 | `POST /auth-service/v1/token` | form — `grant_type=client_credentials`, `appkey`, `appsecret` |
| 2. 현재가 조회 | `POST /trade/v1/domestic/price` | JSON — `{"sym": "A016C000"}`, 헤더 `Authorization: Bearer {토큰}` |

- 환경: 모의 서버 `https://apidemo.futures.co.kr`
- 종목: 코스피200 F 202612 (`A016C000`)

## 실행 방법

```bash
pip install -r requirements.txt
python week01/first_call.py
```

키는 `~/NHFutures/config/config.yaml` 에서 읽습니다(저장소 밖). 서식은 루트의 `config.example.yaml` 과 같습니다.
스크립트는 화면에 키·토큰을 앞뒤 4자만 보여 주고, 응답 원문은 토큰 없이 `results/first_call_YYYYMMDD_HHMMSS.json` 에 저장합니다.

## 실행 근거

`results/` 폴더의 JSON 이 실제 호출 기록입니다. 호출 시각, HTTP 상태, 응답 시간, 현재가 응답 원문이 들어 있습니다.


[전체 일정으로](../README.md)
