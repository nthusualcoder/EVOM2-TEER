# EVOM2 TEER 측정 프로그램

WPI **EVOM2** 상피 전기저항계(TEER meter)의 아날로그 출력(BNC)을 **NI USB-6003** DAQ 로 읽어 PC 에 기록하는 Windows 프로그램입니다.

- 실시간 값 표시, blank 30초 평균, TEER(Ω·cm²) = (R − blank) × 막 면적 자동 계산
- 24/12/6/96-well 플레이트 그림에서 well 을 골라 기록 (왼쪽 위 A1 모서리 모따기)
- 기록 방식: 수동 · 반자동(정한 시간) · **자동**(N초 측정 → M초 휴식 → 바로 옆 well … K개)
- 한 번의 기록을 대표하는 값 = **마지막 3초 평균** (CSV 에는 초당 5행이 모두 남음)
- **시간 추적 그래프**: 폴더 안 CSV(하루 = 파일 하나)를 파일 수정 날짜 순으로 놓고 well 별 패널로 일간 TEER 추이 표시, 표를 CSV 로 내보내기
- **[업데이트 확인]** 버튼: 이 저장소의 최신 릴리스를 받아 스스로 교체

## 설치 (다른 PC)

1. **NI-DAQmx 드라이버** 설치 (무료, 1회): https://www.ni.com/ko-kr/support/downloads/drivers/download.ni-daq-mx.html
2. USB-6003 을 USB 로 연결
3. [최신 릴리스](https://github.com/nthusualcoder/EVOM2-TEER/releases/latest)에서 `EVOM2_TEER.exe` 를 받아 쓰기 가능한 폴더(바탕화면 등)에 두고 실행
   - Python 설치 불필요. SmartScreen 경고가 뜨면 [추가 정보] → [실행], V3 등 백신의 "프로그램 실행 알림"에는 [허용] (서명 없는 exe 라 새 버전마다 뜰 수 있음)
   - 측정 CSV 와 설정 파일은 exe 와 같은 폴더(또는 프로그램에서 고른 폴더)에 생깁니다

배선 (EVOM2 OUTPUT BNC → USB-6003 AI 단자, 기본 DIFF): 노란 선(BNC 중심) → 2번(AI0+), 검은 선(실드) → 3번(AI4 = AI0−)과 1번(AI GND) 둘 다.

자세한 사용법: [`EVOM2 장비 메뉴얼/EVOM2_연결_및_데이터수집_가이드.md`](EVOM2%20장비%20메뉴얼/EVOM2_연결_및_데이터수집_가이드.md)

## 폴더

| 경로 | 내용 |
|---|---|
| `EVOM2 장비 코드/EVOM2_TEER.py` | 프로그램 본체 (tkinter GUI, `APP_VERSION` = 버전) |
| `EVOM2 장비 코드/release.py` | exe 빌드 · GitHub 릴리스 배포 도구 |
| `EVOM2 장비 코드/build_exe.bat` | 더블클릭으로 exe 만들기 |
| `EVOM2 장비 메뉴얼/` | 한국어 사용 가이드, exe 안내, 명령줄 버전(`evom2_teer_logger.py`) |
| `EVOM2_TEER 배포용 (이 폴더만 다른 PC로 복사)/` | USB 로 옮길 때 쓰는 설치 순서 안내 (exe·NI 설치기는 릴리스/NI 사이트에서) |
| `EVOM2_TEER_실행.bat` | Python 이 있는 PC 에서 .py 로 실행 |

저장소에 넣지 않은 것: 측정 데이터(CSV), PC 별 설정(`evom2_settings.json`), 실행 파일(→ 릴리스), 제조사 PDF·NI 설치 프로그램. 제조사 문서는 아래에서 받으세요.

- EVOM2 매뉴얼: https://www.wpi-europe.com/downloads/content/EVOM2_IM%20(1).pdf
- STX2 전극: https://www.wpi-europe.com/downloads/content/STX2_IM.pdf
- NI USB-6001/6002/6003 사용 설명서 (핀 배치 p.9): https://docs-be.ni.com/bundle/usb-6000-6001-6002-6003-features/raw/resource/enus/374259a.pdf

## 개발 · 새 버전 배포

```powershell
cd "EVOM2 장비 코드"
pip install nidaqmx pyinstaller          # 처음 한 번
python EVOM2_TEER.py                     # 실행 (장비 없으면 '시뮬레이션' 선택)
python EVOM2_TEER.py --selftest          # 자체 테스트 -> selftest_result.txt
python release.py build                  # exe 만들기
python release.py publish "바뀐 내용"     # APP_VERSION 을 올린 뒤: 빌드 + commit/tag/push + 릴리스 업로드
```

각 PC 의 프로그램은 켤 때 최신 릴리스를 한 번 확인하고, 새 버전이 있으면 [업데이트 v…] 버튼으로 받아 바꿉니다. 릴리스의 exe 는 받은 뒤 크기와 SHA-256 을 확인하고 교체합니다.
