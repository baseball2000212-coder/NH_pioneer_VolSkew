# VolSkew · NH선물 REST API Pioneer 1기

NH선물 REST API Pioneer 1기(2026.10.21 ~ 12.11) 팀 저장소입니다.
주차마다 `weekNN/` 폴더에 보고서(README.md)·코드·실행 근거를 쌓습니다.

## 팀

| 이름 | 역할 |
|------|------|
| 이승윤 | 전략 설계, 시장·매크로 리서치 |
| 민승기 | 퀀트 모델링, 시스템 구현 |


## 주차 표

| 주차 | 제출(금 23:59) | 주제 | 폴더 |
|------|---------------|------|------|
| 1 | 10/23 | API 키 발급·첫 호출, 팀 저장소 | [week01](week01/README.md) |
| 2 | 10/30 | 국내상품 전략 구현 | week02 |
| 3 | 11/06 | 해외상품·거래소 전략 구현 | week03 |
| 4 | 11/13 | 복수상품·스프레드 전략 (+ 중간 발표) | week04 |
| 5 | 11/20 | 데이터 기반 전략 | week05 |
| 6 | 11/27 | 주문·위험관리 응용 | week06 |
| 7 | 12/04 | 최종 프로젝트 1 | week07 |
| 8 | 12/11 | 최종 프로젝트 2 (+ 최종발표) | week08 |

## 실행 준비

```bash
pip install -r requirements.txt
```

인증정보는 저장소에 두지 않습니다. `config.example.yaml` 을 아래 경로로 복사해 키를 넣습니다.

- Windows: `%USERPROFILE%\NHFutures\config\config.yaml`
- macOS / Linux: `~/NHFutures/config/config.yaml`

## 원칙

- APP Key·Secret·토큰·계좌번호는 저장소·로그·캡처에 남기지 않습니다.
- 모의계좌 주문과 프로그램 내부 시뮬레이션을 구분해 적습니다.
- 보고서 수치는 `results/` 원본에서 읽은 값만 씁니다. 손실도 그대로 적습니다.

## 참고

- [NH선물 REST API 개발자센터](https://apidev.futures.co.kr/)
- [NH FUTURES API 공개 저장소](https://github.com/PetterChanHyuk/nh-futures-open-api)
