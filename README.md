# FSS Grade Uploader

축산물원패스(ekape.or.kr)에서 돼지도체 등급판정결과 엑셀을 자동 다운로드하고,
Azure Data Lake Storage(ADLS)에 직접 업로드하는 자동화 도구입니다.

## 동작 흐름

1. 축산물원패스 로그인 (거래증명통합)
2. 돼지도체위임현황 메뉴 진입
3. 판정기간을 어제 날짜로 설정 후 조회
4. 각 행의 등급판정결과 엑셀 다운로드 (기계판정 제외)
5. 다운로드된 엑셀 파일을 ADLS에 업로드

## 폴더 구조

```
fss-gradeuploader/
├── main.py                # 메인 실행 (다운로드 → 업로드)
├── download_grades.py     # 축산물원패스 다운로드 로직
├── upload_grades.py       # ADLS 업로드 로직
├── .env                   # 환경변수 (로그인 정보, ADLS 설정 등)
├── requirements.txt       # Python 패키지 목록
├── setup.bat              # 초기 설정 (venv, 패키지 설치)
├── run.bat                # 수동 실행
├── setup_scheduler.bat    # Windows 스케줄러 등록
├── downloads/             # 다운로드된 엑셀 파일 (날짜별)
└── logs/                  # 실행 로그
```

## 초기 설정

```bat
cd C:\Users\dnftk\dev\fss\fss-gradeuploader
.\setup.bat
```

setup.bat이 자동으로 처리하는 것:
- Python 가상환경(venv) 생성
- 필요 패키지 설치 (playwright, azure-storage-file-datalake 등)
- Playwright Chromium 브라우저 설치

## 환경변수 (.env)

```env
# 축산물원패스 로그인
EKAPE_ID=아이디
EKAPE_PW=비밀번호

# Azure ADLS 업로드
AZURE_STORAGE_CONNECTION_STRING=연결문자열
AZURE_STORAGE_CONTAINER=컨테이너명

# 스케줄러 실행 시간
SCHEDULE_TIME=08:00
```

## 사용법

### 수동 실행

```bat
.\run.bat
```

### 자동 스케줄링 (매일 자동 실행)

1. `.env`에서 `SCHEDULE_TIME` 설정 (예: `08:00`, `18:30`)
2. 관리자 권한으로 실행:
```bat
.\setup_scheduler.bat
```
3. 시간 변경 시: `.env` 수정 후 `setup_scheduler.bat` 다시 실행

### 스케줄러 현황 확인

**GUI로 확인:**
- Windows 검색 → "작업 스케줄러" 실행
- 왼쪽 "작업 스케줄러 라이브러리" 클릭
- 목록에서 `FSS_GradeUploader` 찾기

**명령어로 확인:**
```bat
schtasks /query /tn "FSS_GradeUploader" /v /fo list
```

**스케줄러 삭제:**
```bat
schtasks /delete /tn "FSS_GradeUploader" /f
```

## 문제 해결

### venv 경로 오류 (폴더명 변경 후)
```bat
Remove-Item -Recurse -Force venv
.\setup.bat
```

### azure 모듈 없음
```bat
venv\Scripts\pip.exe install azure-storage-file-datalake
```
