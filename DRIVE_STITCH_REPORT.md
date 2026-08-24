# Drive P01–P05 스티칭 작업 기록

## 입력 검사

P01–P05 모두 `5952x3968`, `24 fps`, H.264 `yuv420p` 8-bit, BT.709 계열이었다.
따라서 16-bit TIFF·FFV1·ProRes 422 HQ는 출력 컨테이너와 작업 정밀도를 높일 뿐, 소스에
없는 10-bit 정보를 복원하지 않는다.

2026-08-24 분할 ZIP의 P03은 CRC 오류가 있어 사용하지 않았다. CRC 검사를 통과한 이전
분할 ZIP을 `samples/originals/drive-20260814-intact`에 풀어 기준 원본으로 사용했다.

## 리그 자동 보정

동기 기준 프레임에서 SIFT/RANSAC으로 다음 회전값을 계산했다.

| camera | yaw | pitch | roll |
| --- | ---: | ---: | ---: |
| P01 | -76.425° | 1.150° | 1.569° |
| P02 | -38.203° | 0.320° | 0.925° |
| P03 | 0.000° | 0.000° | 0.000° |
| P04 | 38.963° | 0.442° | -0.903° |
| P05 | 77.938° | 0.303° | -1.973° |

인접 구간 인라이어 비율은 84.7–97.9%, 각도 RMS 잔차는 0.31–0.55°였다.

## 검증 출력

- `output/drive_stitch_reference_15360x3968.tif`: 15,360×3,968 16-bit RGB 단일 프레임
- `output/drive_stitch_preview_12f.mkv`: 4,096×1,067, 12 frames, FFV1 16-bit 동영상 테스트
- `configs/drive_5cam_180.calibrated.json`: 보정된 리그와 24 fps 설정
- `configs/drive_5cam_180.prores-hq.json`: ProRes 422 HQ 설정
- `output/drive_alignment_report.json`: 정렬 품질 리포트

원본·TIFF·MOV·MKV·EXR 및 projection cache는 `.gitignore`로 GitHub 커밋에서 제외한다.
