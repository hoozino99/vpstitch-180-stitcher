# VPStitch

## Desktop GUI

Windows에서는 `VP Stitch GUI.bat`를 더블클릭하거나 PowerShell에서 실행합니다.

```powershell
.\.venv\Scripts\vpstitch-gui.exe
```

macOS에서는 `VP Stitch GUI.command`를 더블클릭하거나 Terminal에서 실행합니다.

```bash
.venv/bin/vpstitch-gui
```

macOS와 Windows 모두 동일한 Python/Qt/FFmpeg 파이프라인을 사용합니다. FFmpeg는
기본적으로 `imageio-ffmpeg`에 포함된 실행 파일을 사용하며, 시스템 FFmpeg를 직접
지정해야 할 때는 `VPSTITCH_FFMPEG` 환경변수를 설정할 수 있습니다.

권장 작업 순서:

1. `SELECT 5 VIDEOS`에서 왼쪽 카메라부터 오른쪽 카메라 순서로 원본을 선택합니다.
2. `CHECK INPUTS`로 해상도·FPS·10-bit 이상·컬러 태그를 검사합니다.
3. 고정 피사체가 잘 보이는 시각을 선택하고 `PREVIEW`를 누릅니다.
4. 필요하면 `AUTO ALIGN`을 실행하고 프리뷰를 다시 확인합니다.
5. 캔버스·OCIO·출력 코덱을 설정하고 `RENDER`를 누릅니다.

GUI 프리뷰는 모니터 표시를 위한 8-bit 축소 영상일 뿐입니다. 프리뷰 표시가 최종
uint16/float32 스티칭 및 FFV1/TIFF/EXR 마스터의 정밀도에는 영향을 주지 않습니다.
이 프로그램은 VideoStitch Studio의 소스코드나 UI 자산을 사용한 개조판이 아니라,
독립적으로 구현된 스티칭 엔진과 데스크톱 UI입니다.

5대의 고정 카메라로 촬영한 180° 주행 플레이트를 위한 고비트 심도 오프라인
스티처입니다. 모든 기하 변환과 블렌딩은 8-bit 버퍼를 거치지 않습니다.

현재 구현된 핵심:

- 카메라당 5952×3968, 출력 15360×3968 설정 예제
- 렌더 시 조절 가능한 캔버스(최대 20000×6000), FOV, 중심 yaw/pitch
- 메모리를 제한하는 타일 기반 cylindrical projection
- 고정 리그 투영 맵의 디스크 캐시(프레임마다 삼각함수 재계산 방지)
- pinhole 및 equidistant fisheye 렌즈 모델
- float32 feather blending
- 선택형 DIS optical-flow 중첩부 보정과 forward/backward confidence fallback
- 16-bit RGB 입력·중간·출력
- 원본 컬러스페이스 의미를 유지하는 `passthrough` 모드
- 카메라 입력 → scene-linear 작업공간 → 출력공간 OCIO 변환
- 결정론적 TPDF 디더링
- 16-bit lossless FFV1, ProRes 4444/HQ, HEVC 4:4:4 10-bit 출력

## macOS 호환성과 성능

macOS는 지원 대상입니다. 렌더 엔진은 CUDA나 DirectX에 의존하지 않고 Python,
NumPy, OpenCV, FFmpeg, OpenColorIO를 사용하므로 Apple Silicon과 Intel Mac에서
동일한 설정 파일과 CLI를 사용할 수 있습니다.

스티칭의 주된 병목은 운영체제보다 다음 항목입니다.

- 출력 해상도와 타일 수
- 5개 원본의 디코딩 속도와 저장장치 읽기 속도
- `flow.enabled`를 켰을 때의 CPU optical-flow 분석
- ProRes/HEVC 인코딩 방식

따라서 같은 CPU·SSD·출력 설정이면 Windows와 macOS의 차이는 크지 않으며, 최신
Apple Silicon Mac은 일반적인 CPU 기반 렌더에서 충분히 경쟁력 있습니다. 다만 이
프로젝트는 현재 CUDA/Metal 전용 가속 경로를 사용하지 않으므로 특정 Mac GPU가
자동으로 전체 렌더를 가속하지는 않습니다. 실제 비교는 동일한 5개 입력과
`--frames 24` 테스트로 측정해야 합니다.

## 품질 원칙

`passthrough`는 transfer function이나 primaries를 임의로 바꾸지 않습니다. 하지만 렌즈
왜곡 보정과 스티칭은 필연적으로 픽셀을 재샘플링하므로 압축 비트스트림 또는 원본 코드값의
무변형 복사는 아닙니다. 정확한 scene-linear 블렌딩이 필요하면 OCIO 모드를 사용하십시오.

밴딩 방지를 위한 권장 마스터 출력은 다음 순서입니다.

1. FFV1 `gbrp16le` MKV: RGB 16-bit 무손실 중간 마스터
2. half-float EXR 이미지 시퀀스: VFX 파이프라인용(다음 버전의 스트리밍 writer 대상)
3. ProRes 4444 10-bit: 고품질 실무 교환 포맷
4. HEVC 10-bit: 검수/배포용이며 마스터로는 비추천

ProRes/HEVC 변환에는 FFmpeg `zscale=dither=error_diffusion`을 명시하여 16→10-bit
축소 시 밴딩을 완화합니다.

## 설치

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

macOS/Linux Terminal:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[test]"
```

macOS에서 Finder로 `.command` 파일을 처음 실행할 때 보안 확인이 나오면 파일을
Control-click하여 열기를 선택합니다. 터미널에서 직접 실행할 경우에는 별도 권한
설정 없이 `.venv/bin/vpstitch-gui`를 실행하면 됩니다.

## 정지 프레임 테스트

입력은 카메라 순서대로 16-bit RGB TIFF 또는 half/float EXR을 사용합니다.

```powershell
.\.venv\Scripts\vpstitch.exe stitch-frame `
  --config configs\five_cam_180.sample.json `
  --output output\stitched.tif `
  cam0.tif cam1.tif cam2.tif cam3.tif cam4.tif
```

JSON을 바꾸지 않고 렌더마다 캔버스와 구도를 덮어쓸 수 있습니다.

```powershell
.\.venv\Scripts\vpstitch.exe stitch-frame `
  --config configs\five_cam_180.sample.json `
  --canvas 18000x5000 --h-fov 180 --v-fov 52 `
  --center-yaw 0 --center-pitch -2 `
  --output output\stitched.tif `
  cam0.tif cam1.tif cam2.tif cam3.tif cam4.tif
```

최종 렌더 전에 카메라 투영이 캔버스를 얼마나 채우는지 저해상도 mask로 빠르게 확인할
수 있습니다. `safe_full_width_crop`은 좌우 180° 폭을 유지하면서 검은 상·하단이 없는
보수적인 crop입니다.

```powershell
.\.venv\Scripts\vpstitch.exe analyze-canvas `
  --config configs\five_cam_180.sample.json `
  --canvas 20000x6000 `
  --mask output\coverage.png
```

## 비디오 스티칭

```powershell
.\.venv\Scripts\vpstitch.exe stitch-video `
  --config configs\five_cam_180.sample.json `
  --output output\master.mkv `
  cam0.mov cam1.mov cam2.mov cam3.mov cam4.mov
```

첫 실행은 약 2.5GB의 15K projection-map 캐시를 `.vpstitch-cache`에 생성합니다. 이후
프레임에서는 이 맵을 메모리 매핑하여 재사용합니다. 렌즈·카메라 각도·출력 투영 설정이
바뀌면 설정 해시가 달라져 별도 캐시가 자동 생성됩니다. 렌더 전에 명시적으로 만들 수도
있습니다.

```powershell
.\.venv\Scripts\vpstitch.exe build-maps `
  --config configs\five_cam_180.sample.json
```

현재 비디오 디코더는 각 스트림을 FFmpeg `rgb48le`로 디코딩합니다. 출력 코덱은 JSON의
`video.output_codec`에서 선택합니다.

- `ffv1-16`: 무손실 `gbrp16le`
- `tiff16-sequence`: 무손실 16-bit RGB BigTIFF 프레임 시퀀스
- `exr-half-sequence`: half-float RGB OpenEXR 프레임 시퀀스
- `dpx12-sequence`: 12-bit RGB DPX 프레임 시퀀스
- `prores-4444`: ProRes 4444, `yuv444p10le`
- `prores-hq`: ProRes HQ, `yuv422p10le`
- `h264-mp4-10`: 10-bit H.264 `yuv420p10le` MP4 검수본
- `hevc-444-10`: x265 4:4:4 10-bit

15K–20K 무밴딩 기준 마스터에는 `ffv1-16` 또는 `tiff16-sequence`를 권장합니다.
TIFF 시퀀스는 RGB→YUV 변환과 10-bit 양자화를 완전히 피하고, 출력 폴더에 FPS와
컬러 설정을 담은 `vpstitch_manifest.json`을 함께 생성합니다. 출력 경로에는 파일이
아닌 폴더를 지정합니다.

```json
"video": {
  "fps": 29.97,
  "output_codec": "tiff16-sequence"
}
```

`hevc-444-10`은 표준 HEVC 최대 picture level을 넘는 15K×3968 및 20K×6000
캔버스에 사용할 수 없습니다. 이 크기에서는 설정 로딩 단계에서 명시적으로
거부합니다. ProRes는 실용적인 재생·편집본이지만 FFmpeg `prores_ks` 입력 자체가
10-bit YUV이므로 16-bit RGB 보존 마스터와 동일하게 취급하지 않습니다.

OCIO 출력이 ACEScg 같은 scene-linear 공간이라면 `exr-half-sequence`를 사용하십시오.
이 경로는 0 미만과 1을 넘는 highlight 값을 클리핑하지 않고 half-float RGB로
기록합니다. 반대로 TIFF/FFV1/ProRes/HEVC 정수 출력은 최종 출력 공간이 0–1 범위에
들어오는 Log 또는 display-referred 공간일 때 사용해야 합니다.

20K 작업 전에는 예상 자원을 확인할 수 있습니다.

```powershell
.\.venv\Scripts\vpstitch.exe estimate-resources `
  --config configs\five_cam_180.sample.json --canvas 20000x6000
```

렌더 전에 원본 5개의 실제 pixel format, bit depth, 해상도, FPS, 색 메타데이터 일치를
검사할 수 있습니다. 하나라도 8-bit이면 오류로 처리합니다.

```powershell
.\.venv\Scripts\vpstitch.exe probe-inputs `
  --config configs\five_cam_180.sample.json `
  --output output\input-quality.json `
  cam0.mov cam1.mov cam2.mov cam3.mov cam4.mov
```

`flow.enabled`는 실제 렌즈·외부 파라미터 캘리브레이션이 끝난 뒤 켜십시오. Optical-flow
분석용 grayscale만 8-bit이고, 실제 워핑·블렌딩되는 RGB 샘플은 계속 float32입니다.
현재 DIS 모드는 프리뷰 및 작은 잔여 오차 보정용입니다. 큰 시차와 가려짐이 있는 최종
품질 경로에는 전체 프레임 proxy 기반 flow와 시간축 안정화가 추가로 필요합니다.

## OCIO

카메라마다 입력 컬러스페이스를 지정하고 scene-linear 작업공간에서 블렌딩합니다.

```json
{
  "cameras": [
    {"name": "cam0", "colorspace": "ARRI LogC4", "...": "..."}
  ],
  "color": {
    "mode": "ocio",
    "ocio_config": "C:/color/aces_1.3/config.ocio",
    "working_space": "ACEScg",
    "output_space": "ARRI LogC4",
    "integer_dither": true
  }
}
```

실제 OCIO 색공간 이름은 사용하는 config에 존재해야 합니다. 카메라가 이미 scene-linear
RAW로 디베이어된 경우 해당 linear colorspace를 입력으로 지정하십시오.

config에 들어 있는 정확한 색공간 이름은 다음 명령으로 확인할 수 있습니다.

GUI의 `USE BUILT-IN ACES 2.0 / REC.709` 버튼은 설치된 OpenColorIO의 Studio Config를
사용하여 `Camera Rec.709 → ACEScg → Gamma 2.4 Encoded Rec.709` 작업 설정을 채웁니다.
외부 `.ocio` 파일 없이 사용할 수 있으며, 프로젝트의 실제 입력 인코딩과 납품 색공간에
맞게 이름을 변경할 수 있습니다.

```powershell
.\.venv\Scripts\vpstitch.exe list-ocio-spaces `
  --ocio-config C:\color\aces_1.3\config.ocio
```

OCIO 모드에서는 로그/감마 값에 Lanczos 필터를 적용하지 않도록 전체 카메라 프레임을 먼저
scene-linear 작업공간으로 변환한 뒤 렌즈 투영과 블렌딩을 수행합니다. `passthrough` 모드는
요청대로 입력 transfer function을 해석하지 않고 동일한 코드 공간에서 재샘플링합니다.

## 아직 실제 리그 자료로 결정해야 하는 값

샘플 JSON의 `fx/fy/cx/cy`는 구조를 보여주기 위한 임시값입니다. 실사용 전에 반드시
실제 렌즈 캘리브레이션 값으로 교체해야 합니다.

- 렌즈 모델과 distortion 계수
- 정확한 yaw/pitch/roll
- 카메라별 프레임 오프셋
- 출력 vertical FOV와 crop
- 실제 로그/감마 컬러스페이스 및 FFmpeg 색 메타데이터
- 고정 seam 위치와 feather 폭

### 렌즈 캘리브레이션

각 카메라에서 체커보드가 화면 중앙·가장자리·모서리와 여러 거리/각도에 위치하도록 최소
8장, 권장 15~25장을 촬영합니다. `9x6`은 검은 사각형 수가 아니라 내부 코너 수입니다.

```powershell
.\.venv\Scripts\vpstitch.exe calibrate-lens `
  --model pinhole --pattern 9x6 --square-size 25 `
  --output configs\cam0-lens.json `
  calibration\cam0\*.tif
```

광각/어안 렌즈는 `--model fisheye_equidistant`를 사용합니다. 출력 JSON의 `lens` 객체를
리그 설정의 해당 카메라에 복사하면 됩니다.

## 테스트

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## 5카메라 회전 자동 보정

렌즈 내부 파라미터를 각 카메라 설정에 입력한 다음, 움직임이 없는 순간의 동기화된
기준 프레임 5장을 준비합니다. 인접 카메라에 동시에 보이는 건물, 도로 표식처럼
고정되어 있고 거리가 충분한 디테일이 많을수록 좋습니다. 사람이나 자동차가 화면의
대부분을 차지하는 프레임은 피하십시오.

원본 영상에서 동일 시각의 16-bit TIFF를 추출할 수 있습니다. 설정의 카메라별
`frame_offset`도 적용됩니다.

```powershell
.\.venv\Scripts\vpstitch.exe extract-reference `
  --config configs\five_cam_180.sample.json `
  --time 12.5 --output-dir reference `
  cam0.mov cam1.mov cam2.mov cam3.mov cam4.mov
```

```powershell
.\.venv\Scripts\vpstitch.exe calibrate-rig `
  --config configs\five_cam_180.sample.json `
  --output configs\five_cam_180.calibrated.json `
  --report output\rig-alignment.json `
  reference\cam0.tif reference\cam1.tif reference\cam2.tif `
  reference\cam3.tif reference\cam4.tif
```

이 명령은 원본 컬러 데이터를 변환하지 않습니다. 분석용 축소 이미지에서 SIFT
특징을 찾고, 보정된 렌즈 모델로 특징점을 3D 광선으로 되돌린 뒤 RANSAC으로 인접
카메라의 상대 회전을 구합니다. 중앙 카메라를 절대 방향의 기준으로 유지하면서
나머지 카메라의 `yaw_deg`, `pitch_deg`, `roll_deg`만 새 설정 파일에 기록합니다.

보고서에서 각 인접 쌍의 `inliers`와 `inlier_ratio`가 높고
`rms_angular_error_deg`가 낮아야 합니다. 기본값은 초기 리그 각도에서 12도 이상
벗어나는 해를 거부하므로 잘못된 특징 매칭이 설정을 크게 망가뜨리는 것을 막습니다.
근거리 물체의 시차까지 이 회전 보정만으로 제거할 수는 없으며, 그 부분은 seam 배치와
선택적 optical flow로 처리해야 합니다.

## 실제 Drive P01–P05 검증 기록

실제 5대 주행 소스는 `5952x3968`, `24 fps`, H.264 `yuv420p` 8-bit, BT.709 계열로
검출되었습니다. 따라서 16-bit TIFF, FFV1 또는 ProRes 422 HQ로 내보내도 소스에 없는
10-bit 정보가 새로 생기지는 않습니다. 품질 제한을 명시적으로 인정하고 작업할 때만
`--allow-low-bit-depth`를 사용하십시오.

```powershell
.\.venv\Scripts\vpstitch.exe probe-inputs `
  --allow-low-bit-depth --config configs\drive_5cam_180.calibrated.json `
  P01.mov P02.mov P03.mov P04.mov P05.mov
```

실측 리그 보정값은 `configs/drive_5cam_180.calibrated.json`에 들어 있으며, ProRes HQ
출력 전용 설정은 `configs/drive_5cam_180.prores-hq.json`입니다. 짧은 테스트만 돌릴 때는
`stitch-video --frames 12 --canvas 4096x1067`을 사용하고, 전체 영상은 계산 시간과
저장 용량을 확인한 뒤 실행하십시오. `--frames`는 설정 파일을 바꾸지 않고 프레임 수를
제한합니다.

상세한 원본 검사·정렬 수치·출력 파일 기록은 `DRIVE_STITCH_REPORT.md`를 참고하십시오.
