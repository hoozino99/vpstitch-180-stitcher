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

macOS와 Windows는 같은 프로젝트·Rig Profile·렌더큐 형식을 사용하지만 최종 렌더의
하드웨어 경로는 플랫폼에 맞게 선택합니다. Apple Silicon macOS에서는 Metal과
VideoToolbox를 우선 사용하고, 지원되지 않는 변환이나 코덱은 품질을 유지하는 CPU
경로로 자동 폴백합니다. FFmpeg는 기본적으로 `imageio-ffmpeg`에 포함된 실행 파일을
사용하며, 시스템 FFmpeg를 직접 지정해야 할 때는 `VPSTITCH_FFMPEG` 환경변수를 설정할
수 있습니다.

권장 작업 순서:

1. 시작 화면에서 프로젝트를 만들거나 기존 `project.json`을 엽니다. 프로젝트의 기본
   캔버스와 OCIO Config, Input/Working/Output transform은 네이티브 메뉴의
   `Project > Project Settings…`에서 바꿀 수 있습니다.
2. 왼쪽 `MEDIA POOL`에서 폴더를 만들고 전방 3카메라 `P06–P08` 또는 후방 5카메라
   `P01–P05` 원본을 임포트합니다. 클립은 카메라 번호 순으로 정리되며, 임포트만으로
   타임라인이 자동 생성되지는 않습니다. 폴더를 선택한 뒤 `+ FOLDER`를 누르면 그 안에
   하위 폴더가 생성됩니다. 폴더나 여러 클립을 드래그해 다른 폴더에 놓으면 계층과 순서가
   프로젝트에 저장되며, 우클릭 `Move to Folder` 메뉴로도 같은 이동을 할 수 있습니다.
3. `NEW TIMELINE`에서 이름과 `3 CAM · FRONT · P06–P08` 또는
   `5 CAM · REAR · P01–P05` 레이아웃을 고릅니다. Media Pool에서 완성된 세트를
   먼저 선택했다면 생성과 동시에 플레이트를 넣을 수 있습니다. 그렇지 않으면 생성 후
   클립을 선택하고 우클릭해 활성 타임라인에 추가합니다. 왼쪽 `PLATE SETS`에는 개별
   클립 없이 타임라인만 표시되며, 더블클릭해 작업 타임라인을 전환합니다.
4. Media Pool 클립/폴더나 Plate Sets 타임라인을 선택한 뒤 `Backspace` 또는
   `Delete`를 누르면 프로젝트에서 제거됩니다. 우클릭 메뉴에서도 같은 동작을 할 수
   있으며 확인창을 거치고 원본 영상 파일은 디스크에서 삭제하지 않습니다.
   Media Pool의 폴더·클립 행에는 아이콘, 펼침 화살표, 구분선이 표시되고 드래그 중에는
   이동 항목 카드와 드롭 위치가 강조됩니다.
   `MEDIA POOL`, `PLATE SETS`, `ACTIVE TIMELINE`은 각각 테두리로 구분되며 섹션 사이
   핸들을 위아래로 드래그해 높이를 조절할 수 있습니다. 왼쪽 패널과 프리뷰 사이 핸들도
   좌우로 드래그할 수 있고, 마지막 패널 배치는 앱을 다시 열어도 복원됩니다.
5. 각 타임라인은 기본적으로 프로젝트 해상도와 OCIO 설정을 따르지만
   `Timeline > Timeline Settings…`에서 상속을 끄고 별도로 덮어쓸 수 있습니다.
   OCIO config를 읽으면 Input / Working / Delivery 항목이 해당 config의 전체 컬러스페이스
   목록으로 채워집니다. 드롭다운에서 고르거나 이름 일부를 입력해 필터링할 수 있습니다.
6. embedded SMPTE timecode가 있으면 `TC ALIGN`을 눌러 시작점을 맞추고 가장 짧은
   공통 구간으로 자동 트림합니다.
7. 하나의 타임라인 바에서 전체 스티칭 구간의 IN/OUT을 조절합니다.
8. 고정 피사체가 잘 보이는 시각을 플레이헤드로 선택하고 `QUICK PREVIEW`를 누릅니다.
   현재 저장된 카메라 값으로 플레이헤드의 한 프레임만 최대 2K로 스티칭합니다. 첫 프리뷰
   이후에는 플레이헤드를 끌어 놓을 때 해당 대표 프레임만 다시 확인합니다.
9. 왼쪽 `ACTIVE TIMELINE`에서 플레이트 하나를 선택하면 Inspector가 `PLATE` 탭으로
   전환됩니다. 해당 플레이트의 Position X/Y, Rotation, Scale, 좌·우·상·하 Crop,
   Lens Warp 1–4, 좌·우 Feather를 타임라인별로 미세조정하면 메모리 상주형 1280px
   인터랙티브 프리뷰에 자동 반영됩니다. 숫자 필드를 위아래로 드래그해 값을 스크럽할 수 있고 `Shift`를 누른
   채 드래그하면 10배 정밀하게 조정됩니다. 재생 프록시에서 특정 프레임을 고른 뒤 값을
   바꾸면 현재 프레임을 그대로 얼려 표시하고, 임포트 때 백그라운드로 만든 960×540 이하
   소스 프록시에서 해당 TC 프레임 묶음을 읽습니다. 이후 Transform/Crop/Warp 변경은 수정한 카메라 레이어만
   약 40ms 단위로 다시 투영하며 다른 카메라 레이어와 소스 디코드는 재사용합니다. Feather는
   재투영 없이 블렌드만 갱신합니다. 플레이헤드를 0으로 되돌리거나 매번 CLI를 실행하지
   않습니다. `REFRESH FRAME`은 자동 갱신 실패 시 현재 프레임을 수동 재시도합니다.
10. 카메라 사이에 마젠타/시안 화이트 포인트 차이가 보이면 오른쪽 `COLOR > CAMERA MATCH`에서
    기준 카메라를 고르고 `MATCH`를 누릅니다. Quick Preview의 대표 프레임 한 장과 카메라
    중첩부만 분석해 노출을 유지하는 RGB 보정값을 저장하며, 프레임마다 다시 분석하지 않습니다.
    `Strength`로 강도를 낮출 수 있고 `RESET`은 모든 카메라를 1.0 gain으로 되돌립니다.
11. 공간적인 리그 각도 보정이 필요하면 `AUTO STITCH`를 실행합니다. Quick Preview의
    대표 프레임에서 yaw/pitch/roll을 한 번 계산해 타임라인 전체에 고정 적용하고, 결과
    프리뷰를 자동으로 다시 생성합니다.
12. 미디어를 임포트하면 카메라별 960×540 이하 H.264 소스 프록시를 독립 백그라운드
   작업으로 선제 생성합니다. `TC ALIGN` 뒤에는 이 프록시를 지속 디코딩하며 모든 카메라가
   준비된 완전한 프레임 묶음만 표시합니다. 준비된 뒤에는 첫
   `Play` 또는 `Space`부터 바로 재생되며, `Space` 재생/일시정지, `J/K/L` 연속 역재생·정지·정방향,
   `←/→` 한 프레임 이동, `P` 전체화면을 사용할 수 있습니다. 액티브 타임라인에서
   플레이트를 선택하고 `M`을 누르면 중앙 피벗이 표시되는 Move Mode가 켜집니다.
   이때 방향키는 선택 플레이트를 0.05°씩, `Shift+방향키`는 0.005°씩 미세 이동하며
   `M`을 다시 누르면 종료됩니다. Space로 일시정지한 뒤
   다시 재생하면 현재 위치에서 이어집니다. Auto Stitch 뒤 보정 프리뷰 프레임이 아직
   로딩 중일 때 Play를 눌러도 입력을 버리지 않고, 프리뷰가 끝난 뒤 새 스티치 값으로
   Inspector 조정만으로 스티치 동영상 프록시를 다시 만들지는 않습니다. 따라서 값을 드래그하는 동안
   우하단 `CACHE` 작업이 반복되지 않습니다. 기존 스티치 프록시가 최신이면 QMediaPlayer로
   재생하고, 값이 바뀌어 낡았으면 소스 프록시 + 현재 Transform/Crop/Warp/Color 값을 쓰는
   라이브 GPU/OpenCL 프리뷰로 자동 전환합니다. 라이브 경로는 3캠/5캠을 한 묶음으로 전진·역재생·
   프레임 이동하며, 최종 렌더와 같은 TC 스킵 및 카메라별 수동 프레임 오프셋 계산을 공유합니다.
   `P` 전체화면은 정지 프리뷰와 동영상 모두 별도 최상위 전체화면 창을 사용하며, 종횡비와
   전체 캔버스를 유지한 채 화면 폭에 맞는 최대 크기로 표시합니다.
13. `RENDER NOW` 또는 `ADD TO QUEUE`를 누르면 출력 폴더와 파일명을 확인하는 창이
    열립니다. 큐 항목마다 확정된 전체 출력 경로와 설정 스냅샷을 별도로 보관하므로
    `RENDER ALL` 실행 시 다른 타임라인의 경로나 이름이 섞이지 않습니다. Render Queue
    하단은 `RENDER`와 `RENDER ALL`만 표시하며, 로드/제거는 더블클릭·우클릭 또는
    `Backspace`/`Delete`로 처리합니다.

프로젝트 변경은 작은 JSON 파일에 즉시 원자 저장됩니다. 추가로 내용이 바뀐 경우에만
10분마다 `project.autosave.json` 복구 스냅샷을 갱신하므로 고해상도 영상 디코딩이나
스티칭 렌더 부하를 추가하지 않습니다.

### Rig Profile은 무엇인가

앱에서 예전의 `config` 표기는 `Rig Profile`로 바뀌었습니다. 이 파일은 영상마다
새로 만드는 프로젝트 파일이 아니라 카메라 3대 또는 5대의 렌즈 보정값, 좌우 배치 각도,
기본 캔버스와 출력 설정을 담은 리그 레시피입니다. 앱을 처음 실행하면 실측
`Drive 5-Cam` 프로필이 자동으로 로드되므로 같은 리그로 촬영한 P01–P05 플레이트는
별도 JSON 선택 없이 바로 불러와 작업할 수 있습니다.

P06–P08 세 개만 임포트하면 현재 5카메라 프로필의 좌·중앙·우 카메라를 사용한
3카메라 작업 레이아웃으로 자동 전환합니다. 전방 3카메라 리그의 실제 렌즈나 물리
각도가 후방 5카메라 리그와 다르면 전용 3카메라 `Rig Profile`을 열거나 `AUTO STITCH`로
보정해야 합니다.

`AUTO STITCH`는 동기화된 Quick Preview 프레임을 분석해 카메라별 yaw/pitch/roll을 미세
보정합니다. 렌즈 종류와 왜곡값을 영상만 보고 처음부터 자동 생성하는 기능은 아니며,
다른 카메라·렌즈·물리 배치로 바뀌면 해당 리그의 렌즈 캘리브레이션 프로필이 필요합니다.
Media Pool과 기술값은 기본 화면에서 분리되어 있고, 필요할 때 Inspector와 `PROFILE…`,
`JOBS` 패널에서만 확인합니다.

GUI 프리뷰는 모니터 표시를 위한 8-bit 축소 영상일 뿐입니다. 프리뷰 표시가 최종
uint16/float32 스티칭 및 FFV1/EXR 마스터의 정밀도에는 영향을 주지 않습니다.
Quick Preview는 원본 종횡비를 유지한 채 2K(2048×1152) 안에 맞추므로 크롭이나 늘어남이
없습니다. Inspector를 드래그하는 동안에는 같은 대표 프레임으로 최대 1280×720의
인터랙티브 합성을 사용하고, 원본 프레임과 변경되지 않은 카메라 와프를 메모리에서 재사용합니다.
Apple Silicon의 최종 OCIO 렌더는 카메라 입력 변환·컬러매치·remap·feather blend·출력
변환·디더/정수화를 Metal에서 처리합니다. Windows/OpenCL 또는 Metal을 사용할 수 없는
조합에서는 CPU로 폴백합니다.
소스 프록시는 프리뷰 전용 8-bit 캐시이며 `fps_mode=passthrough`로 프레임 복제·누락을
막습니다. Render Queue와 최종 렌더는 이 파일을 입력으로 사용하지 않고 큐에 잠근
원본 10/12-bit 경로와 설정 스냅샷만 사용합니다.
프리뷰용 카메라 입력도 같은 비율로 먼저 축소해 메모리 사용량을 제한하며,
최종 렌더는 Rig Profile에 지정된 전체 해상도로 처리합니다.
Inspector의 `FIT FULL PLATES`는 모든 카메라의 휘어진 외곽선을 샘플링해 H/V FOV와
캔버스를 3% 여유로 자동 확장합니다. 또한 cylindrical 중심의 가로·세로 픽셀 스케일을
같게 맞춰 잘못된 캔버스 비율로 인한 세로 찌부 현상을 막습니다. Width, Height,
Horizontal/Vertical FOV는 직접 입력할 수 있으며 수동값도 프리뷰와 최종 렌더에 똑같이
적용됩니다. 기본 Drive 5-Cam 프로필은 전체 플레이트 확인용 `20000×5504`,
`H 241.29° / V 59.98°` no-crop 캔버스를 사용합니다.
GUI는 플레이트 임포트 직후 각 소스의 픽셀 포맷과 비트 심도를 자동 감지합니다.
8-bit 소스도 프리뷰와 Auto Stitch를 막지 않으며, Media Pool에
`SOURCE 8-bit → MASTER 10/12-bit`처럼 표시합니다. GUI 최종 출력은 ProRes/H.264/HEVC
10-bit 또는 DPX 12-bit만 제공합니다. 낮은 비트 심도 소스를 10-bit 코덱으로 인코딩해도
원본에 없던 색 정밀도가 복원되는 것은 아닙니다.

Media Pool에서 한 개 또는 여러 클립을 선택해 우클릭한 뒤 `INPUT SETTINGS…`를 열면
DaVinci Resolve처럼 입력 색공간(자동/Rec.709/Rec.2020/Rec.601)과 Video Range
(자동/Video/Full)를 명시할 수 있습니다. 이 값은 프리뷰, Auto Stitch 기준 프레임,
최종 렌더에 동일하게 적용됩니다. 비트 심도와 비트레이트는 파일 자체의 속성이므로
자동 감지해 읽기 전용으로 보여주며 입력 설정에서 임의로 바꾸지 않습니다.
이 프로그램은 VideoStitch Studio의 소스코드나 UI 자산을 사용한 개조판이 아니라,
독립적으로 구현된 스티칭 엔진과 데스크톱 UI입니다.

5대의 고정 카메라로 촬영한 180° 주행 플레이트를 위한 고비트 심도 오프라인
스티처입니다. 모든 기하 변환과 블렌딩은 8-bit 버퍼를 거치지 않습니다.

현재 구현된 핵심:

- 카메라당 5952×3968, 전체 플레이트 확인용 출력 20000×5504 Drive 프로필
- 렌더 시 조절 가능한 캔버스(최대 20000×6000), FOV, 중심 yaw/pitch
- 메모리를 제한하는 타일 기반 cylindrical projection
- 고정 리그 투영 맵의 디스크 캐시(프레임마다 삼각함수 재계산 방지)
- embedded SMPTE TC 기반 3개/5개 플레이트 정렬과 공통 구간 자동 트림
- 전방 P06–P08/후방 P01–P05 네이밍 자동 인식 및 물리 카메라 번호 순서 배치
- drop-frame/non-drop-frame 및 24시간 자정 rollover 처리
- pinhole 및 equidistant fisheye 렌즈 모델
- float32 feather blending
- 선택형 DIS optical-flow 중첩부 보정과 forward/backward confidence fallback
- 16-bit RGB 입력·중간·출력
- 원본 컬러스페이스 의미를 유지하는 `passthrough` 모드
- 카메라 입력 → scene-linear 작업공간 → 출력공간 OCIO 변환
- 결정론적 TPDF 디더링
- 16-bit lossless FFV1, ProRes 4444/HQ, HEVC 4:4:4 10-bit 출력

## macOS 호환성과 성능

macOS는 지원 대상입니다. Apple Silicon 앱은 네이티브 arm64 Metal 라이브러리를 포함하며,
OCIO 입력 변환과 카메라 컬러매치, 고정 리그 remap, feather blend, OCIO 출력 변환,
디더와 uint16 양자화를 GPU에서 연속 처리합니다. 고정 투영 맵과 seam weight는 첫 프레임에
한 번만 GPU에 올리고 이후 모든 프레임에서 재사용합니다. Metal을 쓸 수 없는 설정은 같은
정밀도의 NumPy/OpenCV/OpenColorIO CPU 경로로 자동 전환됩니다.

스티칭의 주된 병목은 운영체제보다 다음 항목입니다.

- 출력 해상도와 타일 수
- 5개 원본의 디코딩 속도와 저장장치 읽기 속도
- `flow.enabled`를 켰을 때의 CPU optical-flow 분석
- ProRes/HEVC 인코딩 방식

macOS 기본값은 `VPSTITCH_GPU_BACKEND=auto`이며 OCIO·uint16·optical-flow off 조건에서
Metal 최종 렌더를 선택합니다. `VPSTITCH_GPU_BACKEND=metal|cpu`로 진단용 강제 선택이
가능합니다. 기본 Metal 재샘플러는 품질과 속도의 균형이 좋은 cubic이며,
`VPSTITCH_METAL_FILTER=lanczos4`로 더 느린 Lanczos4 비교 렌더를 실행할 수 있습니다.
Windows와 CPU 폴백의 remap 진단은 기존
`VPSTITCH_REMAP_BACKEND=cpu|opencl|auto`를 사용합니다.

macOS의 OCIO·ProRes HQ·limited-range 출력은 스티치된 RGB16 프레임을 CPU로 복사하거나
FFmpeg stdin으로 보내지 않습니다. Metal이 10-bit 4:2:2 `x422`로 변환한 뒤 IOSurface
메모리를 `MTLBuffer`로 직접 매핑하고 Apple AVFoundation/VideoToolbox ProRes writer에
전달합니다. 20000px 출력은 단일 Metal 텍스처 최대 폭 16384px를 넘으므로 텍스처 대신
stride와 plane offset을 가진 IOSurface buffer를 사용합니다. P3-D65 PQ, Rec.2020 PQ/HLG,
Rec.709처럼 primaries·transfer·non-constant-luminance matrix가 명시되고 Apple
메타데이터로 정확히 표현되는 조합만 자동 선택합니다. BT.2020 constant-luminance,
V-Log처럼 표준 태그가 없는 조합·full-range·비 OCIO 출력은 기존 FFmpeg 고정밀 경로를
유지합니다. 실제 pixel-buffer pool과 IOSurface/Metal mapping도 프레임 처리 전에
preflight하므로 지원되지 않는 장치에서는 출력 파일을 시작하기 전에 fallback합니다.
진단 시 `VPSTITCH_NATIVE_PRORES=off`로 기존 경로를 강제하거나
`VPSTITCH_NATIVE_PRORES=force`로 네이티브 초기화 실패를 즉시 확인할 수 있습니다.

15K~20K 출력, OCIO 또는 optical flow를 사용하면 Apple Silicon에서도 CPU·메모리·SSD
부하가 크게 걸리는 정상적인 오프라인 렌더입니다. 현재는 타일 처리, 디스크 기반 투영
맵 재사용, decoder frame-buffer 재사용, bounded 메모리, TC/트림 시작점의 정확한
frame-index trim이 적용되어 있습니다. 동기화된 입력 프레임 한 묶음이 시스템 메모리 기반
자동 예산 안에 들어오면 한 프레임 선읽기로 디코딩과 Metal 렌더를 겹치며, 예산을 넘으면
bounded 순차 경로로 자동 전환합니다. FFmpeg 오류 출력은 256 KiB 링버퍼로 계속 배출해 장시간 렌더의 파이프
정체를 방지합니다.
가장 큰 속도 개선은 `flow.enabled=false`로 먼저 렌더하고, 반복 작업에서는 동일한
projection cache를 유지하며, 프리뷰를 작은 canvas로 확인한 뒤 마스터를 렌더하는 것입니다.
임포트 프록시는 macOS에서 VideoToolbox, Windows에서 NVENC/QSV/AMF를 가용 순서대로
시도하고 초기화가 실패하면 자동으로 `libx264`로 되돌아갑니다. 이 경로는 저해상도
8-bit 재생 프록시에만 적용되며 10/12-bit 마스터 렌더 품질에는 영향을 주지 않습니다.

개발 기준 M3 Pro(18-core GPU), 5952×3968 5카메라, 20000×5504 P3-PQ 출력에서 네이티브
경로의 60프레임 실측은 초기 셰이더·고정 맵 준비를 포함해 60.17초였습니다. 첫 프레임은
약 33초, 이후 지속 구간은 약 0.46초/프레임이므로 24 fps 1분은 같은 조건에서 약 12분으로
추정됩니다. 기존 RGB48→FFmpeg→ProRes 경로는 약 0.9초/프레임이었습니다. 작은 프레임
5,000개 soak에서 FD는 9개로 고정되고 RSS는 워밍업 후 약 90MB로 안정됐으며, 실제 20K
60프레임에서도 렌더 안정 구간 RSS는 약 5.52GB로 증가 추세가 없었습니다. 저장장치·소스
코덱·flow 설정과 열 상태에 따라 실제 시간은 달라집니다.

## 품질 원칙

`passthrough`는 transfer function이나 primaries를 임의로 바꾸지 않습니다. 하지만 렌즈
왜곡 보정과 스티칭은 필연적으로 픽셀을 재샘플링하므로 압축 비트스트림 또는 원본 코드값의
무변형 복사는 아닙니다. 정확한 scene-linear 블렌딩이 필요하면 OCIO 모드를 사용하십시오.

밴딩 방지를 위한 권장 마스터 출력은 다음 순서입니다.

1. FFV1 `gbrp16le` MKV: RGB 16-bit 무손실 중간 마스터
2. half-float EXR 이미지 시퀀스: VFX 파이프라인용
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

### 다운로드해서 바로 실행하는 macOS 앱

GitHub의 **Actions → macOS app** 워크플로를 수동 실행하거나 `v*` 태그를 만들면
Apple Silicon과 Intel Mac용 `.dmg` 및 `.zip`이 생성됩니다. 압축을 풀고
`VP Stitch.app`을 Applications 폴더로 드래그한 뒤 더블클릭하면 됩니다.
처음 실행할 때 macOS가 개발자를 확인할 수 없다고 표시하면 앱을
Control-click하여 **열기**를 선택하거나, 시스템 설정의 **개인정보 보호 및
보안**에서 실행을 허용하십시오.

Downloads, Documents, 외장 디스크 또는 네트워크 볼륨의 원본 플레이트를 처음 열 때는
macOS 파일 접근 요청에서 **허용**을 선택하십시오. 이전에 거부했다면 **시스템 설정 →
개인정보 보호 및 보안 → 파일 및 폴더 → VP Stitch**에서 해당 위치를 켜야 합니다.
권한 때문에 FFmpeg가 소스를 열지 못하면 앱은 무한 대기하지 않고 15초 안에 이 설정을
안내하는 오류를 표시합니다.

앱은 최근 프로젝트 목록 같은 전역 설정을 다음 사용자 폴더에 저장합니다.

```text
~/Library/Application Support/VP-LAB/VP Stitch/
```

프로젝트 데이터, 프록시, projection cache, 렌더 큐와 출력은 프로젝트 생성 시 선택한
폴더 아래에 저장합니다.

앱을 실행할 때 Python, FFmpeg, OpenColorIO를 따로 설치할 필요는 없습니다. 앱에
렌더 CLI와 정확한 TC/frame count scan이 가능한 정적 FFmpeg가 함께 포함됩니다.

현재 macOS 앱은 Qt Cocoa의 접근성 계층 조회 중 발생하는 네이티브 크래시를 피하기
위해 Qt 위젯의 VoiceOver/자동화 하위 트리를 비활성화합니다. 일반 마우스·키보드
작업과 렌더에는 영향이 없습니다. 접근성 트리가 반드시 필요하면 Terminal에서
`VPSTITCH_ENABLE_ACCESSIBILITY=1`을 설정해 실행할 수 있지만, Qt 쪽 문제가
해결되기 전까지는 안정성 모드를 권장합니다.

### 다운로드해서 바로 실행하는 Windows 앱

GitHub의 **Actions → Windows app** 워크플로를 수동 실행하거나 `v*` 태그를 만들면
`VP-Stitch-Windows-*.zip`이 생성됩니다. 압축을 푼 폴더 안의 `VP Stitch.exe`를
실행하면 되며 Python, FFmpeg, OpenColorIO를 따로 설치할 필요가 없습니다. Windows
배포 ZIP은 아직 코드 서명되지 않았으므로 SmartScreen이 표시되면 파일 출처를 확인한
후 실행을 허용하십시오.

macOS/Linux에서 CLI를 직접 사용할 때는 Windows 예시의
`.venv\\Scripts\\vpstitch.exe`를 `.venv/bin/vpstitch`로 바꾸고, 줄 연결은
PowerShell의 백틱(`) 대신 역슬래시(`\\`)를 사용하십시오.

## SMPTE TC 정렬과 공통 듀레이션

GUI의 `TC ALIGN`은 video stream, container 또는 QuickTime `tmcd` metadata의
timecode를 읽습니다. 가장 늦게 시작한 플레이트를 공통 시작점으로 선택하고, 앞서
시작한 플레이트는 필요한 프레임만큼 건너뜁니다. 이후 남아 있는 프레임 수가 가장
짧은 플레이트를 공통 OUT으로 사용하므로 앞뒤로 1~3프레임 차이가 있어도 출력 길이가
자동으로 맞습니다. TC가 없거나 FPS가 서로 다르면 추측하지 않고 오류로 표시합니다.
가장 짧은 구간을 프레임 단위로 확정하기 위해 번들 FFmpeg가 실제 video frame을
확인합니다. CFR 영상에서 `nb_frames`, duration, FPS가 서로 일치하면 메타데이터만 읽는
고속 경로를 사용하므로 5개 클립도 보통 1초 안팎입니다. VFR이거나 메타데이터가 없거나
서로 모순되면 정확도를 위해 기존 전체 프레임 count 경로로 자동 폴백합니다.

## 프록시 플레이백과 렌더 큐

`AUTO STITCH`는 선택한 대표 프레임에서 카메라 회전을 한 번 계산하고, 최종 영상은 그
고정 투영 맵을 모든 프레임에 재사용합니다. 프록시 플레이백 역시 같은 맵을 사용하지만
원본을 약 960px로 디코드하고 optical flow를 끈 8-bit H.264 검수 프록시이므로 최종
10/12-bit 마스터 품질에는 영향을 주지 않습니다. TC Align 직후 전체 선택 구간을 미리
생성하며, 스티치·컬러·캔버스·IN/OUT 값이 바뀌면 마지막 대표 프레임 갱신을 우선하고
새 설정의 프록시를 한 번만 다시 만듭니다. 투영 맵은 고정 재사용하지만 영상 픽셀은 각
프레임마다 decode/warp/blend/encode해야 하므로 최초 캐시 준비 시간 자체는 필요합니다.

최종 영상 렌더는 숨겨진 staging 경로에 먼저 완성한 뒤 검증된 파일이나 시퀀스 폴더만
사용자가 지정한 이름으로 원자적으로 이동합니다. 실패·취소 시 불완전한 결과가 최종
파일명으로 남지 않으며 Render Queue 상태도 최종 이동이 끝난 뒤에만 DONE이 됩니다.

Render Queue의 각 행은 사진 스냅숏이 아니라 타임라인의 `Settings lock`입니다. 소스 경로,
소스 순서, 정확한 FPS, TC 정렬, IN/OUT, 카메라 보정, 캔버스, OCIO, 코덱과 출력 경로를
큐 등록 순간에 독립 저장합니다. 기본 `Match each plate set` 정책은 각 타임라인의 플레이트
FPS를 감지해 23.976과 24.000을 구분하며, 같은 플레이트 세트 안의 FPS가 섞이면 큐 등록을
막습니다. 의도적인 변환만 타임라인 설정의 `Custom conform`으로 지정할 수 있습니다.
현재 프로젝트 설정은
다음에 임포트하는 테이크의 기본값으로 이어지고, 큐의 타임라인을 `LOAD`하면 해당
Settings lock을 다시 열어 새 작업으로 수정할 수 있습니다. 큐 상태는 사용자 데이터 폴더의
`render-queue.json`에 원자적으로 저장되며 앱이 렌더 도중 종료되면 해당 작업은 다음
실행에서 `QUEUED`로 복구됩니다. 동일한 리그/캔버스의 작업들은 projection-map 캐시를
공유합니다. 큐 등록 시 설정에는 `Settings lock` 해시가 붙습니다. 이후 다른 타임라인이나
현재 UI 값을 변경해도 이미 등록된 작업은 바뀌지 않으며, 렌더 직전 생성된 전체 해상도
설정 JSON이 잠긴 설정과 완전히 같은지 다시 검증한 뒤에만 렌더를 시작합니다.

CLI에서 정렬 계획만 JSON으로 확인할 수도 있습니다.

```powershell
.\.venv\Scripts\vpstitch.exe align-timecode `
  --config configs\five_cam_180.sample.json `
  --output output\timecode-alignment.json `
  cam0.mov cam1.mov cam2.mov cam3.mov cam4.mov
```

공통 타임라인에서 추가로 48프레임을 건너뛰고 240프레임만 렌더하려면 다음과 같이
실행합니다.

```powershell
.\.venv\Scripts\vpstitch.exe stitch-video `
  --config configs\five_cam_180.sample.json `
  --alignment-plan output\timecode-alignment.json `
  --start-frame 48 --frames 240 `
  --output output\stitched.mkv `
  cam0.mov cam1.mov cam2.mov cam3.mov cam4.mov
```

## 정지 프레임 테스트

입력은 카메라 순서대로 16-bit RGB TIFF 또는 half/float EXR을 사용합니다.

```powershell
.\.venv\Scripts\vpstitch.exe stitch-frame `
  --config configs\five_cam_180.sample.json `
  --output output\stitched.png `
  cam0.tif cam1.tif cam2.tif cam3.tif cam4.tif
```

JSON을 바꾸지 않고 렌더마다 캔버스와 구도를 덮어쓸 수 있습니다.

```powershell
.\.venv\Scripts\vpstitch.exe stitch-frame `
  --config configs\five_cam_180.sample.json `
  --canvas 18000x5000 --h-fov 180 --v-fov 52 `
  --center-yaw 0 --center-pitch -2 `
  --output output\stitched.png `
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
- `exr-half-sequence`: half-float RGB OpenEXR 프레임 시퀀스
- `dpx12-sequence`: 12-bit RGB DPX 프레임 시퀀스
- `prores-4444`: ProRes 4444, `yuv444p10le`
- `prores-hq`: ProRes HQ, `yuv422p10le`
- `h264-mp4-10`: 10-bit H.264 `yuv420p10le` MP4 검수본
- `hevc-444-10`: x265 4:4:4 10-bit

15K–20K 무밴딩 기준 영상 마스터에는 `ffv1-16`을 권장합니다. VFX 프레임 교환이
필요한 경우에만 `exr-half-sequence` 또는 `dpx12-sequence`를 선택합니다.

```json
"video": {
  "fps": 29.97,
  "output_codec": "ffv1-16"
}
```

`hevc-444-10`은 표준 HEVC 최대 picture level을 넘는 15K×3968 및 20K×6000
캔버스에 사용할 수 없습니다. 이 크기에서는 설정 로딩 단계에서 명시적으로
거부합니다. ProRes는 실용적인 재생·편집본이지만 FFmpeg `prores_ks` 입력 자체가
10-bit YUV이므로 16-bit RGB 보존 마스터와 동일하게 취급하지 않습니다.

OCIO 출력이 ACEScg 같은 scene-linear 공간이라면 `exr-half-sequence`를 사용하십시오.
이 경로는 0 미만과 1을 넘는 highlight 값을 클리핑하지 않고 half-float RGB로
기록합니다. 반대로 FFV1/ProRes/HEVC 정수 출력은 최종 출력 공간이 0–1 범위에
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
    "ocio_config": "vpstitch://aces-studio-v4.0.0",
    "working_space": "ACEScg",
    "output_mode": "display_view",
    "display": "Rec.2100-PQ - Display",
    "view": "ACES 2.0 - HDR 1000 nits (Rec.2020)",
    "match_enabled": true,
    "match_reference": "cam0",
    "match_space": "ACEScg",
    "match_strength": 1.0,
    "integer_dither": true
  }
}
```

실제 OCIO 색공간 이름은 사용하는 config에 존재해야 합니다. 카메라가 이미 scene-linear
RAW로 디베이어된 경우 해당 linear colorspace를 입력으로 지정하십시오.

config에 들어 있는 정확한 색공간 이름은 다음 명령으로 확인할 수 있습니다.

앱에는 Academy Software Foundation이 배포한 공식
`OpenColorIO-Config-ACES v4.0.0 / ACES 2.0 / OCIO 2.5` Studio Config가 포함됩니다.
새 프로젝트는 `vpstitch://aces-studio-v4.0.0`을 기본값으로 사용하고, 앱은 이를
macOS/Windows 패키지 내부의 실제 `.ocio` 파일로 해석합니다. 따라서 인터넷 연결이나
별도 OCIO 다운로드가 필요 없고 프로젝트를 다른 OS로 옮겨도 앱 설치 경로가 저장되지
않습니다. `USE BUNDLED ACES 2.0 / REC.709` 버튼은
`Camera Rec.709 → ACEScg → Gamma 2.4 Encoded Rec.709` 작업 설정을 채웁니다.
프로젝트의 실제 입력 인코딩과 납품 색공간에 맞게 이름을 변경할 수 있습니다.

`CAMERA MATCH`는 입력을 OCIO의 `scene_linear` 역할(기본 ACEScg)로 변환한 뒤 카메라
중첩부의 색도 차이만 robust하게 계산합니다. 넓은 실제 seam은 픽셀 수와 신뢰도만큼 더
강하게 반영하고, 가장자리의 좁은 우연 겹침은 전체 매치값을 끌어당기지 않습니다. 기준
카메라는 gain 1.0으로 고정하고 나머지 카메라의 RGB gain은 밝기가 변하지 않도록
정규화합니다. 따라서 같은 매치값을 Rec.2100 PQ 1000 nit,
P3-D65 PQ 1000 nit, Rec.709 또는 V-Log 납품에 재사용할 수 있습니다. 단, 입력 컬러스페이스나
Working Space를 바꾸면 기존 매치값은 자동으로 해제되며 새 Quick Preview에서 다시 MATCH해야
합니다. 카메라별 노출 차이까지 자동 보정하지는 않습니다.

HDR 마스터는 `Delivery method: Display transform`에서 `P3-D65 PQ` 또는
`Rec.2020 PQ`와 ACES 2.0 1000 nit View를 선택합니다. P3-PQ/Rec.2020-PQ 출력은
FFmpeg primaries/transfer/matrix 메타데이터도 함께 설정됩니다. `Apple Display P3 HDR`은
Apple EDR/sRGB-piecewise 모니터 인코딩이며 ST2084/PQ 파일 납품이 아닙니다. V-Log처럼
scene/log 인코딩을 내보낼 때는 `Delivery method: Color space / Log`에서 맨 위에 배치된
`V-Log V-Gamut`을 선택합니다.

`COLOR > Viewer monitor`는 로컬 검수 화면에만 적용됩니다. 일반 SDR 모니터에서는
`Standard Rec.709`를 사용합니다. 내부 ACES 변환은 표준 SDR 기준 ODT를 사용하지만 UI에서는
모니터의 별도 nit 프로파일을 요구하지 않습니다.
최종 Delivery가 P3-PQ/Rec.2020-PQ 1000 nits 또는 V-Log여도 Quick Preview와 재생 프록시만
선택한 SDR 모니터 변환을 사용합니다. Viewer 설정은 최종 렌더 config와 Render Queue의
Settings lock에 기록되지 않으므로 이미 큐에 넣은 납품 컬러스페이스를 바꾸지 않습니다.

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

원본 영상에서 동일 시각의 16-bit PNG 기준 프레임을 추출할 수 있습니다. 설정의 카메라별
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
  reference\cam0.png reference\cam1.png reference\cam2.png `
  reference\cam3.png reference\cam4.png
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
검출되었습니다. GUI는 이를 자동 인식해 프리뷰를 허용하고 ProRes 422 HQ 10-bit를
기본 마스터로 사용합니다. 다만 소스에 없는 10-bit 정보가 새로 생기지는 않습니다.
CLI에서 같은 동작이 필요할 때만 `--allow-low-bit-depth`를 사용하십시오.

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
