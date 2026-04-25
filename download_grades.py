"""
축산물원패스 등급판정결과 자동 다운로드 스크립트
- fastLogin.jsp 페이지에서 거래증명통합으로 로그인
- 돼지도체위임현황 메뉴에서 어제 날짜 등급판정결과 엑셀 다운로드
"""

import os
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("ekape_download.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ===== 설정 =====
EKAPE_LOGIN_URL = "https://www.ekape.or.kr/kapecp/ui/kapecp/fastLogin.jsp"
EKAPE_MAIN_URL = "https://www.ekape.or.kr/kapecp/ui/kapecp/index.html"
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "./downloads")

# 다운로드 결과 상태 코드
STATUS_OK = "ok"                  # 정상 다운로드
STATUS_NO_DATA = "no_data"        # 조회 결과가 0건 (정상 케이스)
STATUS_CONFIG_MISSING = "config_missing"  # 계정정보 누락
STATUS_LOGIN_FAILED = "login_failed"      # 로그인 실패
STATUS_NAV_FAILED = "nav_failed"          # 메뉴 이동 실패
STATUS_SEARCH_FAILED = "search_failed"    # 조회 실패
STATUS_ERROR = "error"                    # 기타 예외

# Nexacro 요소 ID (끝 부분으로 매칭)
NEXACRO_ID_INPUT = 'input[id$="edtUserId:input"]'
NEXACRO_PW_INPUT = 'input[id$="edtPswrd:input"]'
NEXACRO_LOGIN_BTN = 'div[id$="btnLogin"]'
NEXACRO_TRADE_TAB = 'div[id$="btnLoginType01"]'


def get_target_date_str():
    """대상 날짜를 반환 (TARGET_DATE 환경변수 우선, 없으면 어제)"""
    target = os.getenv("TARGET_DATE", "").strip()
    if target:
        logger.info(f"TARGET_DATE 환경변수 사용: {target}")
        return target
    yesterday = datetime.now() - timedelta(days=1)
    return yesterday.strftime("%Y-%m-%d")


def ensure_download_dir():
    """다운로드 폴더를 날짜별로 생성"""
    today_str = datetime.now().strftime("%Y%m%d")
    download_path = Path(DOWNLOAD_DIR) / today_str
    download_path.mkdir(parents=True, exist_ok=True)
    return str(download_path.resolve())


def dismiss_all_popups(page):
    """Nexacro 팝업/모달을 닫기 (DOM 삭제 없이 안전하게)"""
    logger.info("팝업 닫기 시도 중...")
    for attempt in range(3):
        try:
            close_btns = page.get_by_text("닫기").all()
            for btn in close_btns:
                try:
                    btn.click(force=True, timeout=2000)
                    logger.info("  '닫기' 버튼 클릭")
                    time.sleep(1)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            page.keyboard.press("Escape")
            time.sleep(1)
        except Exception:
            pass
        try:
            page.evaluate("""() => {
                document.querySelectorAll('.nexamodaloverlay').forEach(el => {
                    el.style.display = 'none';
                });
            }""")
        except Exception:
            pass
    time.sleep(1)


def resolve_ekape_credentials():
    """축산물원패스 계정정보 조회.

    우선순위:
        1. KEY_VAULT_URL 설정 시 → Azure Key Vault에서 조회 (Managed Identity 인증)
        2. 그 외 → EKAPE_ID / EKAPE_PW 환경변수

    Returns:
        (ekape_id, ekape_pw, error_message)
    """
    vault_url = os.getenv("KEY_VAULT_URL", "").strip()
    if vault_url:
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient

            id_secret = os.getenv("EKAPE_ID_SECRET_NAME", "EKAPE-ID")
            pw_secret = os.getenv("EKAPE_PW_SECRET_NAME", "EKAPE-PW")

            client = SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())
            ekape_id = client.get_secret(id_secret).value
            ekape_pw = client.get_secret(pw_secret).value
            logger.info(f"계정정보 로드: Key Vault ({vault_url}, {id_secret}/{pw_secret})")
            return ekape_id, ekape_pw, None
        except Exception as e:
            err = f"Key Vault에서 계정정보 조회 실패: {e}"
            logger.error(err)
            return None, None, err

    ekape_id = os.getenv("EKAPE_ID", "").strip()
    ekape_pw = os.getenv("EKAPE_PW", "").strip()
    if ekape_id and ekape_pw:
        logger.info("계정정보 로드: 환경변수 (.env)")
        return ekape_id, ekape_pw, None

    return None, None, "EKAPE_ID/EKAPE_PW 환경변수가 없고 KEY_VAULT_URL도 설정되지 않았습니다."


def login_ekape(page, ekape_id, ekape_pw):
    """축산물원패스 로그인 (거래증명통합) - fastLogin.jsp 사용"""
    logger.info("축산물원패스 로그인 시작...")
    page.goto(EKAPE_LOGIN_URL, wait_until="networkidle", timeout=60000)
    time.sleep(3)

    try:
        logout_btn = page.get_by_text("로그아웃")
        if logout_btn.is_visible(timeout=3000):
            logger.info("이미 로그인 되어 있습니다.")
            return True
    except Exception:
        pass

    try:
        trade_tab = page.locator(NEXACRO_TRADE_TAB).first
        if trade_tab.is_visible(timeout=3000):
            trade_tab.click()
            time.sleep(1)
            logger.info("거래증명통합 탭 선택")
    except Exception:
        logger.info("거래증명통합 탭이 이미 선택되어 있거나 찾을 수 없음")

    try:
        id_input = page.locator(NEXACRO_ID_INPUT)
        id_input.wait_for(state="visible", timeout=10000)
        id_input.click()
        id_input.fill(ekape_id)
        logger.info("아이디 입력 완료")
    except Exception as e:
        logger.error(f"아이디 입력 실패: {e}")
        return False

    try:
        pw_input = page.locator(NEXACRO_PW_INPUT)
        pw_input.wait_for(state="visible", timeout=5000)
        pw_input.click()
        pw_input.fill(ekape_pw)
        logger.info("비밀번호 입력 완료")
    except Exception as e:
        logger.error(f"비밀번호 입력 실패: {e}")
        return False

    time.sleep(1)

    try:
        login_btn = page.locator(NEXACRO_LOGIN_BTN).first
        login_btn.click()
        logger.info("로그인 버튼 클릭")
    except Exception as e:
        logger.error(f"로그인 버튼 클릭 실패: {e}")
        return False

    time.sleep(10)

    for attempt in range(3):
        try:
            logout_btn = page.get_by_text("로그아웃")
            if logout_btn.is_visible(timeout=5000):
                logger.info("로그인 성공!")
                return True
        except Exception:
            pass

        current_url = page.url
        if "index.html" in current_url and "fastLogin" not in current_url:
            logger.info(f"로그인 성공! (URL 확인)")
            return True

        try:
            id_input = page.locator(NEXACRO_ID_INPUT)
            if not id_input.is_visible(timeout=2000):
                logger.info("로그인 성공! (로그인 폼 사라짐)")
                return True
        except Exception:
            pass

        logger.info(f"로그인 확인 대기 중... ({attempt + 1}/3)")
        time.sleep(5)

    if "fastLogin" not in page.url:
        logger.info("로그인 성공으로 간주 (로그인 페이지 아님)")
        return True

    logger.error("로그인 실패!")
    return False


def navigate_to_pig_delegation(page):
    """돼지도체위임현황 메뉴로 이동"""
    logger.info("돼지도체위임현황 메뉴로 이동 중...")

    try:
        menu = page.get_by_text("등급판정결과", exact=True).first
        menu.click()
        time.sleep(2)
        logger.info("등급판정결과 메뉴 클릭")
    except Exception as e:
        logger.error(f"등급판정결과 메뉴 클릭 실패: {e}")
        return False

    try:
        submenu = page.get_by_text("돼지도체위임현황", exact=True).first
        submenu.click()
        time.sleep(5)
        logger.info("돼지도체위임현황 서브메뉴 클릭")
    except Exception as e:
        logger.error(f"돼지도체위임현황 서브메뉴 클릭 실패: {e}")
        return False

    dismiss_all_popups(page)
    return True


def set_date_and_search(page, target_date):
    """판정기간을 어제 날짜로 설정하고 조회 (정확한 Nexacro 셀렉터 사용)"""
    logger.info(f"판정기간을 {target_date}로 설정 중...")

    dismiss_all_popups(page)
    time.sleep(1)

    date_selectors = [
        ('시작일', 'input[id*="divCalFromTo"][id*="calFrom"][id$=":input"]'),
        ('종료일', 'input[id*="divCalFromTo"][id*="calTo"][id$=":input"]'),
    ]

    for label, selector in date_selectors:
        try:
            inp = page.query_selector(selector)
            if not inp:
                logger.warning(f"  판정기간 {label} 필드를 찾을 수 없습니다: {selector}")
                continue

            old_val = inp.input_value()
            logger.info(f"  판정기간 {label} 현재값: {old_val}")

            inp.click(force=True)
            time.sleep(0.3)
            page.keyboard.press("Control+a")
            time.sleep(0.2)
            page.keyboard.type(target_date, delay=50)
            time.sleep(0.3)
            page.keyboard.press("Tab")
            time.sleep(0.5)

            new_val = inp.input_value()
            if new_val == target_date:
                logger.info(f"  판정기간 {label} 설정 완료: {old_val} -> {new_val}")
            else:
                logger.warning(f"  판정기간 {label} 값 불일치: 기대={target_date}, 실제={new_val}")
        except Exception as e:
            logger.warning(f"  판정기간 {label} 설정 실패: {e}")

    time.sleep(1)

    try:
        search_btn = page.query_selector('div[id*="tabFar"][id*="btnSearch"]')
        if not search_btn:
            search_btn = page.get_by_text("조회", exact=True).first
        search_btn.click(force=True)
        time.sleep(7)
        logger.info("조회 버튼 클릭, 결과 로딩 대기 중...")
    except Exception as e:
        logger.error(f"조회 버튼 클릭 실패: {e}")
        return False

    return True


def download_all_grade_results(page, download_path):
    """모든 행의 등급판정결과를 다운로드 (cell_X_12 = 등급판정결과, cell_X_13 = 기계판정)"""
    logger.info("등급판정결과 다운로드 시작...")

    dismiss_all_popups(page)

    downloaded_files = []

    grade_buttons = page.query_selector_all(
        'div[id*="grdMndtMngmCowList.body"][id$="_12.cellbutton"]'
    )
    logger.info(f"등급판정결과 다운로드 버튼(cell_X_12) {len(grade_buttons)}개 발견")

    visible_buttons = []
    for btn in grade_buttons:
        try:
            box = btn.bounding_box()
            if box and box["width"] > 0 and box["height"] > 0:
                visible_buttons.append(btn)
        except Exception:
            continue

    logger.info(f"보이는 등급판정결과 다운로드 버튼 {len(visible_buttons)}개")

    if len(visible_buttons) == 0:
        logger.warning("다운로드 버튼이 없습니다. 조회 결과를 확인하세요.")
        return []

    for idx, btn in enumerate(visible_buttons):
        try:
            logger.info(f"  [{idx + 1}/{len(visible_buttons)}] 다운로드 중...")

            btn.scroll_into_view_if_needed()
            time.sleep(0.5)

            with page.expect_download(timeout=30000) as download_info:
                btn.click(force=True)

            download = download_info.value
            filename = download.suggested_filename or f"grade_result_{idx + 1}.xls"
            save_path = os.path.join(download_path, filename)

            base, ext = os.path.splitext(save_path)
            counter = 1
            while os.path.exists(save_path):
                save_path = f"{base}_{counter}{ext}"
                counter += 1

            download.save_as(save_path)
            downloaded_files.append(save_path)
            logger.info(f"  -> 저장 완료: {save_path}")

            time.sleep(2)

        except PlaywrightTimeout:
            logger.warning(f"  [{idx + 1}] 다운로드 타임아웃 - 데이터가 없을 수 있습니다")
        except Exception as e:
            logger.error(f"  [{idx + 1}] 다운로드 실패: {e}")

    logger.info(f"총 {len(downloaded_files)}개 파일 다운로드 완료")
    return downloaded_files


def run_download():
    """메인 다운로드 실행 함수

    Returns:
        dict: {"files": [...], "status": STATUS_*, "error": str | None}
    """
    ekape_id, ekape_pw, cred_err = resolve_ekape_credentials()
    if cred_err:
        return {"files": [], "status": STATUS_CONFIG_MISSING, "error": cred_err}

    yesterday = get_target_date_str()
    download_path = ensure_download_dir()
    logger.info(f"=== 등급판정결과 다운로드 시작 (대상 날짜: {yesterday}) ===")
    logger.info(f"다운로드 경로: {download_path}")

    with sync_playwright() as p:
        is_ci = os.getenv("CI", "false").lower() == "true"
        browser = p.chromium.launch(
            headless=is_ci,
            args=["--start-maximized"] if not is_ci else [],
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            accept_downloads=True,
        )
        page = context.new_page()

        try:
            if not login_ekape(page, ekape_id, ekape_pw):
                logger.error("로그인 실패! 프로세스를 종료합니다.")
                return {"files": [], "status": STATUS_LOGIN_FAILED, "error": "축산물원패스 로그인 실패"}

            if not navigate_to_pig_delegation(page):
                logger.error("메뉴 이동 실패! 프로세스를 종료합니다.")
                return {"files": [], "status": STATUS_NAV_FAILED, "error": "돼지도체위임현황 메뉴 이동 실패"}

            if not set_date_and_search(page, yesterday):
                logger.error("조회 실패! 프로세스를 종료합니다.")
                return {"files": [], "status": STATUS_SEARCH_FAILED, "error": "판정기간 조회 실패"}

            downloaded = download_all_grade_results(page, download_path)
            status = STATUS_OK if downloaded else STATUS_NO_DATA
            return {"files": downloaded, "status": status, "error": None}

        except Exception as e:
            logger.error(f"예상치 못한 오류: {e}")
            try:
                page.screenshot(path=os.path.join(download_path, "error_screenshot.png"))
            except Exception:
                pass
            return {"files": [], "status": STATUS_ERROR, "error": str(e)}

        finally:
            browser.close()
            logger.info("브라우저 종료")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    result = run_download()
    downloaded_files = result["files"]

    if downloaded_files:
        logger.info(f"\n=== 다운로드 완료 ===")
        for f in downloaded_files:
            logger.info(f"  - {f}")
    else:
        logger.warning(f"다운로드된 파일이 없습니다. (status={result['status']}, error={result['error']})")
