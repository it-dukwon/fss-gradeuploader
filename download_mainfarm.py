"""
축산물원패스 '돼지도체'(본장 = 덕원농장 직접) 등급판정결과 다운로드 — download-only.

기존 download_grades.py(돼지도체위임현황 = 위탁계열 농장)와 별개 크롤러.
- 메뉴: 등급판정결과 → 돼지도체 (위임현황 아님)
- 본장(덕원농장) 데이터. 평상=18시 컷오프 단일일, 백필=MAINFARM_START_DATE/END_DATE 범위(연도별).

흐름 (라이브 실측 확정):
  돼지도체 메인창(winSN00030200)에서 '판정일자' 범위로 조회
   → 결과 그리드 grdGradeJudgeRst(바인딩 dsGradeJudgeRstList, 행 = 작업장+판정일자 '1배치')
   → 각 행마다 상세팝업(CPCA202) 열고 '등급판정결과 내역 저장'(btnExcel) 으로 .xls 저장 → 닫기 반복.
  ※ 상세팝업엔 자체 날짜필터가 없고, 날짜는 메인창에서만 세팅. export는 '행 단위'.
  ※ Nexacro 컴포넌트는 DOM :input 직접세팅이 안 먹어 set_value()/함수호출로 제어한다.
     창 suffix(_0_958)는 가변이라 winSN00030200 서브트리로 스코프해 컴포넌트를 찾는다.

양식이 위임현황과 달라 엑셀 처리는 webapp 책임(요건: WEBAPP_mainfarm_excel_요건.md).
"""

import os
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from download_grades import (
    resolve_ekape_credentials,
    login_ekape,
    dismiss_all_popups,
    KST,
    STATUS_OK,
    STATUS_NO_DATA,
    STATUS_CONFIG_MISSING,
    STATUS_LOGIN_FAILED,
    STATUS_NAV_FAILED,
    STATUS_SEARCH_FAILED,
    STATUS_ERROR,
)

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "./downloads")
# 평상 운영 대상일 컷오프(KST 시): 이 시각 '이전' 실행=어제 / '이후'=오늘. (본장 기준 18시)
MAINFARM_CUTOFF_HOUR = int(os.getenv("MAINFARM_CUTOFF_HOUR", "18"))


def resolve_date_range():
    """조회 기간 (start_s, end_s) 결정.
      1) MAINFARM_START_DATE + MAINFARM_END_DATE → 백필 범위(연도별)
      2) TARGET_DATE                            → 단일 지정일
      3) 평상: 18시 컷오프 단일일 (이전=어제 / 이후=오늘)
    """
    s = os.getenv("MAINFARM_START_DATE", "").strip()
    e = os.getenv("MAINFARM_END_DATE", "").strip()
    if s and e:
        return s, e
    t = os.getenv("TARGET_DATE", "").strip()
    if t:
        return t, t
    now = datetime.now(KST)
    d = now if now.hour >= MAINFARM_CUTOFF_HOUR else (now - timedelta(days=1))
    ds = d.strftime("%Y-%m-%d")
    return ds, ds


def _log_xls_summary(path):
    """다운로드 .xls 의 sheet/컬럼/행수 + 앞 몇 행 로그 (검증 + webapp 컬럼매핑 용)."""
    try:
        import xlrd
        wb = xlrd.open_workbook(path)
        sh = wb.sheet_by_index(0)
        logger.info(f"[xls] sheet='{sh.name}' nrows={sh.nrows} ncols={sh.ncols}")
        for r in range(min(4, sh.nrows)):
            vals = [str(sh.cell_value(r, c)).strip() for c in range(sh.ncols)]
            logger.info(f"[xls] row{r}: {vals}")
    except Exception as e:
        sz = os.path.getsize(path) if os.path.exists(path) else "?"
        logger.warning(f"[xls] 요약 실패(형식 확인 필요): {e} / size={sz}")


def navigate_to_mainfarm(page):
    """등급판정결과 → 돼지도체 메뉴 이동.

    '돼지도체' 텍스트는 로그인 앱타일에도 있어 단순 get_by_text 가 오매칭 → 좌측 메뉴(frameLeft) 스코프.
    """
    logger.info("돼지도체(본장) 메뉴로 이동 중...")
    try:
        page.get_by_text("등급판정결과", exact=True).first.click(force=True)
        time.sleep(2)
        logger.info("등급판정결과 메뉴 클릭")
    except Exception as e:
        logger.error(f"등급판정결과 메뉴 클릭 실패: {e}")
        return False

    clicked = False
    candidates = [
        lambda: page.locator('[id*="frameLeft"]').get_by_text("돼지도체", exact=True).first,
        lambda: page.get_by_text("돼지도체", exact=True).last,
    ]
    for i, getter in enumerate(candidates):
        try:
            getter().click(force=True, timeout=8000)
            clicked = True
            logger.info(f"돼지도체 서브메뉴 클릭 (후보 {i})")
            break
        except Exception as e:
            logger.warning(f"돼지도체 서브메뉴 후보 {i} 실패: {str(e)[:120]}")

    time.sleep(5)
    dismiss_all_popups(page)
    if not clicked:
        logger.error("돼지도체 서브메뉴 클릭 실패(모든 후보)")
        return False
    return True


def search_main_grid(page, start_date, end_date):
    """메인창(winSN00030200)에서 판정일자 범위로 조회. 행(작업장) 클릭 전에 호출."""
    s_ymd = start_date.replace("-", "")
    e_ymd = end_date.replace("-", "")
    result = page.evaluate(
        """([s, e]) => {
            if (typeof nexacro === 'undefined' || !nexacro.getApplication) return 'no-nexacro';
            const app = nexacro.getApplication();
            let form = null;
            (function walk(o, d, inT) {
                if (!o || d > 16 || form) return;
                if (inT && o.divWork && o.divWork.form && o.divWork.form.divSearch
                        && o.divWork.form.divSearch.form
                        && o.divWork.form.divSearch.form.dt) {
                    form = o.divWork.form.divSearch.form; return;
                }
                for (const k of Object.keys(o)) {
                    if (form) return;
                    const v = o[k];
                    if (v && typeof v === 'object') {
                        const t2 = inT || /winSN00030200/i.test(k);
                        if (k === 'form' || /Frame|frameset|win|div|CPCA|VFrameSet|HFrameSet/i.test(k)) {
                            try { walk(v, d + 1, t2); } catch (err) {}
                        }
                    }
                }
            })(app.mainframe, 0, false);
            if (!form) return 'no-form(winSN00030200)';
            try {
                if (form.srchDtGubun) form.srchDtGubun.set_value('01');  // 01=판정일자
                form.dt.form.calFrom.set_value(s);   // yyyyMMdd
                form.dt.form.calTo.set_value(e);
                form.btnSearch.click();
                return 'ok';
            } catch (err) { return 'err:' + (err && err.message); }
        }""",
        [s_ymd, e_ymd],
    )
    logger.info(f"메인 조회 set_value 결과: {result} (판정일자 {start_date} ~ {end_date})")
    time.sleep(8)  # 그리드 재조회 로딩 대기
    return result


def get_grade_row_count(page):
    """결과 그리드(dsGradeJudgeRstList) 행 수 반환. (-1=폼 미발견)"""
    return page.evaluate(
        """() => {
            if (typeof nexacro === 'undefined' || !nexacro.getApplication) return -1;
            const app = nexacro.getApplication();
            let grd = null;
            (function walk(o, d, inT) {
                if (!o || d > 16 || grd) return;
                if (inT && o.divWork && o.divWork.form && o.divWork.form.grdGradeJudgeRst) {
                    grd = o.divWork.form.grdGradeJudgeRst; return;
                }
                for (const k of Object.keys(o)) {
                    if (grd) return;
                    const v = o[k];
                    if (v && typeof v === 'object') {
                        const t2 = inT || /winSN00030200/i.test(k);
                        if (k === 'form' || /Frame|frameset|win|div|CPCA|VFrameSet|HFrameSet/i.test(k)) {
                            try { walk(v, d + 1, t2); } catch (err) {}
                        }
                    }
                }
            })(app.mainframe, 0, false);
            if (!grd) return -1;
            const ds = grd.getBindDataset();
            return ds ? ds.getRowCount() : -1;
        }"""
    )


# ─────────────────────────────────────────────────────────────
# 공통 JS: winSN00030200 서브트리에서 작업폼(divWork.form) 찾기.
# ※ 화살표함수 '본문 안'에 삽입해서 쓴다 (앞에 붙이면 evaluate 가 단일식으로 못 읽음).
# ─────────────────────────────────────────────────────────────
_FIND_WORKFORM_JS = """
function findWorkForm() {
    if (typeof nexacro === 'undefined' || !nexacro.getApplication) return null;
    const app = nexacro.getApplication();
    let wf = null;
    (function walk(o, d, inT) {
        if (!o || d > 16 || wf) return;
        if (inT && o.divWork && o.divWork.form && o.divWork.form.grdGradeJudgeRst) {
            wf = o.divWork.form; return;
        }
        for (const k of Object.keys(o)) {
            if (wf) return;
            const v = o[k];
            if (v && typeof v === 'object') {
                const t2 = inT || /winSN00030200/i.test(k);
                if (k === 'form' || /Frame|frameset|win|div|CPCA|VFrameSet|HFrameSet/i.test(k)) {
                    try { walk(v, d + 1, t2); } catch (e) {}
                }
            }
        }
    })(app.mainframe, 0, false);
    return wf;
}
"""


def _popup_state(page):
    """현재 떠 있는 상세팝업의 form key('CPCA201'|'CPCA202')와 btnExcel/btnClose 생존 여부."""
    return page.evaluate(
        """() => {
            if (typeof nexacro === 'undefined' || !nexacro.getApplication) return {key:null};
            const app = nexacro.getApplication();
            let res = {key:null, hasExcel:false, hasClose:false};
            (function walk(o, d, inT) {
                if (!o || d > 16 || res.key) return;
                if (inT) {
                    for (const nm of ['CPCA201','CPCA202']) {
                        try {
                            const p = o[nm];
                            if (p && p.form && (p.form.btnExcel || p.form.btnClose)) {
                                res = {key:nm, hasExcel:!!p.form.btnExcel, hasClose:!!p.form.btnClose};
                                return;
                            }
                        } catch (e) {}
                    }
                }
                for (const k of Object.keys(o)) {
                    if (res.key) return;
                    const v = o[k];
                    if (v && typeof v === 'object') {
                        const t2 = inT || /winSN00030200/i.test(k);
                        if (k === 'form' || /Frame|frameset|win|div|CPCA|VFrameSet|HFrameSet/i.test(k)) {
                            try { walk(v, d + 1, t2); } catch (e) {}
                        }
                    }
                }
            })(app.mainframe, 0, false);
            return res;
        }"""
    )


def _wait_popup_gone(page, timeout_s=6):
    """CPCA201/202 상세팝업이 사라질 때까지 폴링. True=닫힘 확인."""
    end = time.time() + timeout_s
    while time.time() < end:
        st = _popup_state(page)
        if not st or not st.get("key"):
            return True
        time.sleep(0.2)
    return False


def open_detail_popup_for_row(page, row_index):
    """가상스크롤 그리드에서 row_index 행을 화면에 띄우고, 그 행의 작업장(컬럼3) 셀을
    '실제 DOM 클릭'해 상세팝업(CPCA201/202)을 viewport 안에 정상 오픈한다.

    핵심(라이브 진단으로 확정):
      - 결과그리드는 가상스크롤(렌더 band 약 22개 고정, 데이터만 이동).
      - ds.set_rowposition(rowIdx) 으로 스크롤되지만 비동기 애니메이션이라 topRow API
        (_getScreenTopRowPos)가 즉시엔 transient(잘못된) 값을 준다 → topRow 산수로 클릭하면
        엉뚱한 행을 다운로드할 위험.
      - 그래서 topRow 에 의존하지 않고, 정착 대기 후 '렌더된 band 중 (작업장 abattName +
        판정일자 judgeDate)가 일치하는 band'(각 행이 유니크)를 찾아 그 셀을 클릭한다(자가교정).
    반환: 'ok' | 실패문자열.
    """
    # 1) rowposition 세팅 → 그리드가 해당 행으로 스크롤
    setres = page.evaluate(
        "(rowIdx) => {" + _FIND_WORKFORM_JS + """
            const wf = findWorkForm();
            if (!wf) return {err:'no-workform'};
            const grd = wf.grdGradeJudgeRst;
            const ds = grd.getBindDataset();
            if (!ds || rowIdx >= ds.getRowCount()) return {err:'row-oob'};
            ds.set_rowposition(rowIdx);
            try { if (grd.setCellPos) grd.setCellPos(3); } catch (e) {}
            return {ok:true};
        }""",
        row_index,
    )
    if not isinstance(setres, dict) or not setres.get("ok"):
        return f"scroll-fail:{setres}"

    # 2) 정착 대기 후 (작업장+판정일자) 일치 band 탐색 → 그 셀 실제 클릭. 미정착 시 재시도.
    find_js = "(rowIdx) => {" + _FIND_WORKFORM_JS + """
        const wf = findWorkForm(); if (!wf) return {screenRow:-1, via:'no-wf'};
        const grd = wf.grdGradeJudgeRst;
        const ds = grd.getBindDataset();
        const targetAbatt = String(ds.getColumn(rowIdx, 'abattName') || '');
        const targetDate  = String(ds.getColumn(rowIdx, 'judgeDate') || '');
        const dvars = [targetDate, targetDate.replace(/-/g,'.'), targetDate.replace(/-/g,'')];
        function bandText(b) {
            let t = '';
            document.querySelectorAll('[id*="grdGradeJudgeRst.body"][id*="gridrow_'+b+'.cell_"]')
                .forEach(c => { t += ' ' + (c.textContent || ''); });
            return t;
        }
        function matches(b) {
            const t = bandText(b);
            if (!targetAbatt || t.indexOf(targetAbatt) < 0) return false;
            return dvars.some(d => d && t.indexOf(d) >= 0);
        }
        let tp = 0;
        try { const r = grd._getScreenTopRowPos ? grd._getScreenTopRowPos() : null;
              if (Array.isArray(r)) tp = r[0]; else if (typeof r === 'number') tp = r; } catch (e) {}
        const sr = rowIdx - tp;
        if (sr >= 0 && matches(sr)) return {screenRow: sr, via:'calc', abatt:targetAbatt, date:targetDate};
        const bands = new Set();
        document.querySelectorAll('[id*="grdGradeJudgeRst.body"][id*="gridrow_"]')
            .forEach(c => { const m = c.id.match(/gridrow_(\\d+)/); if (m) bands.add(parseInt(m[1])); });
        for (const b of bands) { if (matches(b)) return {screenRow: b, via:'scan', abatt:targetAbatt, date:targetDate}; }
        return {screenRow: -1, via:'none', abatt:targetAbatt, date:targetDate};
    }"""

    clicked = False
    last = {}
    for _ in range(4):
        time.sleep(0.45)  # 스크롤 애니메이션 정착 대기
        info = page.evaluate(find_js, row_index)
        last = info if isinstance(info, dict) else {}
        sr = last.get("screenRow", -1)
        if not isinstance(sr, int) or sr < 0:
            continue
        cell = page.query_selector(f'div[id*="grdGradeJudgeRst.body"][id$="cell_{sr}_3"]')
        if not cell:
            continue
        try:
            cell.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            pass
        try:
            box = cell.bounding_box()
        except Exception:
            box = None
        if not box or box["width"] <= 0 or box["height"] <= 0:
            continue
        try:
            cell.click(timeout=4000)
            clicked = True
            break
        except Exception:
            try:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                clicked = True
                break
            except Exception:
                continue

    if not clicked:
        logger.warning(f"  행 {row_index} 셀 미클릭 (last={last})")
        return f"cell-dom-not-clickable({last.get('via')})"

    # 3) 팝업 btnExcel DOM 가시화 대기
    for _ in range(50):  # ~10초
        btn = page.query_selector('div[id$="CPCA201.form.btnExcel"], div[id$="CPCA202.form.btnExcel"]')
        if btn:
            try:
                bb = btn.bounding_box()
                if bb and bb["width"] > 0 and bb["height"] > 0:
                    return "ok"
            except Exception:
                pass
        time.sleep(0.2)
    return "popup-no-btnexcel"


def _close_detail_popup(page, force=False):
    """상세팝업(CPCA201/202) 닫기.
    1순위: btnClose 의 '실제 DOM' 클릭(열기/다운로드와 동일한 검증된 경로).
    2순위(force 또는 DOM 실패): 컴포넌트 btnClose.click() → pop.close()/destroy() 폴백.
    """
    if not force:
        for sel in ('div[id$="CPCA201.form.btnClose"]', 'div[id$="CPCA202.form.btnClose"]'):
            btn = page.query_selector(sel)
            if not btn:
                continue
            try:
                box = btn.bounding_box()
            except Exception:
                box = None
            if not box or box["width"] <= 0 or box["height"] <= 0:
                continue
            try:
                btn.click(timeout=3000)
                logger.info("  팝업 닫기: dom-click")
                return
            except Exception:
                try:
                    btn.click(force=True, timeout=3000)
                    logger.info("  팝업 닫기: dom-click(force)")
                    return
                except Exception as e:
                    logger.warning(f"  팝업 닫기(DOM) 실패: {str(e)[:80]}")

    try:
        r = page.evaluate(
            """() => {
                if (typeof nexacro === 'undefined' || !nexacro.getApplication) return 'no-nexacro';
                const app = nexacro.getApplication();
                let pop = null, name = '';
                (function walk(o, d, inT) {
                    if (!o || d > 16 || pop) return;
                    if (inT) {
                        for (const nm of ['CPCA201','CPCA202']) {
                            try { if (o[nm] && o[nm].form) { pop = o[nm]; name = nm; return; } } catch (e) {}
                        }
                    }
                    for (const k of Object.keys(o)) {
                        if (pop) return;
                        const v = o[k];
                        if (v && typeof v === 'object') {
                            const t2 = inT || /winSN00030200/i.test(k);
                            if (k === 'form' || /Frame|frameset|win|div|CPCA|VFrameSet|HFrameSet/i.test(k)) {
                                try { walk(v, d + 1, t2); } catch (e) {}
                            }
                        }
                    }
                })(app.mainframe, 0, false);
                if (!pop) return 'no-popup';
                try { if (pop.form && pop.form.btnClose && pop.form.btnClose.click) { pop.form.btnClose.click(); } } catch (e) {}
                try { if (pop.close)   { pop.close();   return 'win-close:' + name; } } catch (e) {}
                try { if (pop.destroy) { pop.destroy(); return 'destroy:'  + name; } } catch (e) {}
                return 'comp-btnClose:' + name;
            }"""
        )
        logger.info(f"  팝업 닫기(comp): {r}")
    except Exception as e:
        logger.warning(f"팝업 닫기 오류: {e}")


def download_mainfarm_results(page, download_path, start_date, end_date):
    """판정일자 범위로 메인 조회 → 각 행마다 상세팝업 → btnExcel 저장 → 닫기 반복."""
    logger.info("돼지도체 다운로드 시작...")
    dismiss_all_popups(page)
    downloaded_files = []

    page.on("dialog", lambda d: d.accept())  # 저장 확인창 자동 수락

    # --- 1) 메인창에서 판정일자 범위로 조회 (행 클릭 전에!) ---
    if search_main_grid(page, start_date, end_date) != "ok":
        logger.error("메인 조회 실패 — 중단")
        return downloaded_files

    row_count = get_grade_row_count(page)
    logger.info(f"조회 결과 행 수: {row_count}")
    if row_count is None or row_count <= 0:
        logger.warning(f"결과 없음 (row_count={row_count})")
        return downloaded_files

    # --- 2) 행 단위 반복: 상세팝업(실제 DOM 클릭) → btnExcel 저장 → 닫기 ---
    for i in range(int(row_count)):
        logger.info(f"[{i + 1}/{row_count}] 행 처리 중...")

        opened = open_detail_popup_for_row(page, i)
        if opened != "ok":
            logger.warning(f"  행 {i} 팝업 오픈 실패({opened}) — 닫기 시도 후 건너뜀")
            _close_detail_popup(page)        # 떠버린 팝업 정리(다음 행 already-exists 방지)
            _wait_popup_gone(page, timeout_s=4)
            continue

        # btnExcel '실제 DOM' 가시 요소 확보
        save_btn = None
        for _ in range(40):  # ~8초
            cand = page.query_selector('div[id$="CPCA201.form.btnExcel"], div[id$="CPCA202.form.btnExcel"]')
            if cand:
                try:
                    box = cand.bounding_box()
                    if box and box["width"] > 0 and box["height"] > 0:
                        save_btn = cand
                        break
                except Exception:
                    pass
            time.sleep(0.2)
        if save_btn is None:
            logger.warning(f"  행 {i}: btnExcel 미발견/화면밖 — 닫기 후 건너뜀")
            _close_detail_popup(page)
            _wait_popup_gone(page, timeout_s=4)
            continue

        try:
            # ★ 검증된 유일 경로: 실제 DOM 클릭 + expect_download (download_grades.py 동일)
            try:
                save_btn.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            with page.expect_download(timeout=30000) as dl_info:
                try:
                    save_btn.click(timeout=5000)
                except Exception:
                    save_btn.click(force=True, timeout=5000)  # viewport 가드 우회 폴백
            dl = dl_info.value
            fname = dl.suggested_filename or f"mainfarm_grade_{i}.xls"
            save_path = os.path.join(download_path, fname)
            base, ext = os.path.splitext(save_path)
            n = 1
            while os.path.exists(save_path):
                save_path = f"{base}_{n}{ext}"
                n += 1
            dl.save_as(save_path)
            downloaded_files.append(save_path)
            logger.info(f"  -> 저장 완료: {save_path}")
            if i == 0:
                _log_xls_summary(save_path)  # 첫 행만 요약(컬럼 확인용)
        except Exception as e:
            logger.error(f"  행 {i} 다운로드 실패: {e}")
        finally:
            _close_detail_popup(page)
            if not _wait_popup_gone(page, timeout_s=6):
                logger.warning(f"  행 {i}: 팝업 미닫힘 — 강제 닫기 재시도")
                _close_detail_popup(page, force=True)
                _wait_popup_gone(page, timeout_s=4)
            time.sleep(0.8)

    logger.info(f"총 {len(downloaded_files)}개 파일 다운로드 완료")
    return downloaded_files


def run_mainfarm_download():
    """돼지도체(본장) 다운로드 메인. download-only.

    Returns dict: {files, status, error, start, end}
    """
    ekape_id, ekape_pw, cred_err = resolve_ekape_credentials()
    if cred_err:
        return {"files": [], "status": STATUS_CONFIG_MISSING, "error": cred_err}

    start_s, end_s = resolve_date_range()
    dl_dir = Path(DOWNLOAD_DIR) / ("mainfarm_" + end_s.replace("-", ""))
    dl_dir.mkdir(parents=True, exist_ok=True)
    download_path = str(dl_dir.resolve())

    rng = f"{start_s} ~ {end_s}" if start_s != end_s else f"{end_s} (단일일)"
    logger.info(f"=== 돼지도체(본장) 다운로드 시작 (대상 {rng}) ===")
    logger.info(f"다운로드 경로: {download_path}")

    force_headless = os.getenv("HEADLESS", "false").lower() == "true"
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=force_headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1920,1080", "--start-maximized"],
        )
        context = browser.new_context(viewport={"width": 1920, "height": 1080}, accept_downloads=True)
        page = context.new_page()
        try:
            if not login_ekape(page, ekape_id, ekape_pw):
                return {"files": [], "status": STATUS_LOGIN_FAILED, "error": "로그인 실패", "start": start_s, "end": end_s}
            if not navigate_to_mainfarm(page):
                return {"files": [], "status": STATUS_NAV_FAILED, "error": "돼지도체 메뉴 이동 실패", "start": start_s, "end": end_s}

            files = download_mainfarm_results(page, download_path, start_s, end_s)
            status = STATUS_OK if files else STATUS_NO_DATA
            logger.info(f"돼지도체 다운로드 결과: {len(files)}건 (status={status})")
            return {"files": files, "status": status, "error": None, "start": start_s, "end": end_s}
        except Exception as e:
            logger.error(f"돼지도체 다운로드 예외: {e}")
            return {"files": [], "status": STATUS_ERROR, "error": str(e), "start": start_s, "end": end_s}
        finally:
            try:
                context.close()
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()  # 로컬: .env 의 KEY_VAULT_URL / EKAPE_ID·PW / MAINFARM_* 로드
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler("mainfarm_run.log", encoding="utf-8", mode="w"),
            logging.StreamHandler(),
        ],
    )
    print(run_mainfarm_download())
