# ⌨️ Keyboard Performance Course

사요디바이스 스타일의 **CMD 코스형 키보드 성능 점검 툴**입니다.
메뉴를 고르는 방식이 아니라, 타이틀 화면 이후 **전 항목을 순서대로 진행**하고 마지막에 테스트 기록을 **JSON으로 저장할지 물어봅니다.**

```
[타이틀] → 키 아무거나 → STAGE 1 → 결과 → 키 아무거나 → STAGE 2 → ... → JSON 저장 여부 질문
```

## 코스 구성

| # | 단계 | 측정 내용 |
|---|------|-----------|
| 1 | 자석축 / 래피드 트리거 | 0.0~4.0mm 실시간 스트로크 그래프(60fps), KeyDown(초록●)/KeyUp(빨강●) 마커, 최상단·바닥 센서 노이즈, 데드존 채터링 |
| 2 | 폴링레이트 | Δt 기반 실시간 Hz, 최소/평균/최대 지연(ms) |
| 3 | 초단타(Micro-tap) | 1~3ms 입력 Down→Up 쌍 누락, 5ms 내 재입력(채터링) |
| 4 | KPS / UR | 연타(Jack)·교대(Stream) 최고 KPS, 입력 간격 편차, UR |
| 5 | 노브(Rotary Encoder) | 좌/우 회전 스텝, 클릭 |
| 6 | HID Raw 디버거 | VID/PID, 원시 패킷 Hex 실시간 표시 (점수 없음) |

측정할 수 없는 단계(프로파일 없음 등)는 건너뜁니다.

## 설치 & 실행

```bash
pip install -r requirements.txt
python main.py
```

- Windows CMD / PowerShell / Windows Terminal 권장 (ANSI 컬러 사용)
- Linux는 `keyboard` 모듈 때문에 `sudo`가 필요합니다
- 코스가 끝나면 `JSON으로 출력할까요? (y/n)` 을 묻고, `y`면 `results/` 폴더에 저장합니다

## 아날로그(자석축) · HID 설정

표준 OS 키 입력만으로는 **키 깊이(mm)** 와 **정확한 8000Hz 패킷 간격**을 알 수 없습니다.
이 두 가지는 키보드가 보내는 HID 리포트를 직접 읽어야 하므로 **프로파일**이 필요합니다.

`profiles/default.json` 은 **직접 만드는 설정 파일**입니다. 실행하면 자동으로 읽으며, 없으면 프로파일 없이(일부 단계 건너뜀) 진행합니다.

1. 연결된 기기 확인
   ```bash
   python main.py --list-hid
   ```
2. `profiles/example.json` 을 `profiles/default.json` 으로 복사 후 수정

| 키 | 설명 |
|----|------|
| `vendor_id`, `product_id` | 10진수 값 (`--list-hid` 출력의 0x값을 변환) |
| `interface_number` | 아날로그 리포트가 나오는 인터페이스 (모르면 `null`) |
| `key_offset`, `value_bytes`, `endian` | 리포트 안에서 키 깊이 값의 위치/크기/엔디안 |
| `max_raw` | 완전히 눌렸을 때의 raw 값 |
| `travel_mm` | 최대 키 스트로크 (기본 4.0) |
| `test_key` | 그래프 마커에 사용할 키 이름 |
| `knob` | 노브가 보내는 키 이름 (`left` / `right` / `click`) |

패킷 구조는 6단계 **HID Raw 디버거**로 직접 확인해서 `key_offset` 등을 맞추세요.
`--profile 경로.json` 으로 다른 프로파일도 지정할 수 있습니다.

## 한계 (정직하게)

- 프로파일이 없으면 폴링레이트는 **OS 이벤트 간격 근사치**라 8000Hz는 과소측정될 수 있습니다.
- Windows에서 일부 키보드의 HID 인터페이스는 OS가 독점해 직접 읽기가 안 될 수 있습니다.
- 1~3ms 초단타는 OS 타임스탬프 지터 영향을 받습니다.
- KPS/UR은 사람의 손 실력이 섞여 키보드 단독 성능이 아닙니다.

## 파일 구조

```
main.py            본체
title.txt          타이틀 ASCII 아트
profiles/          HID 프로파일
requirements.txt
```

## License

MIT
