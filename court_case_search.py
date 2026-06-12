"""대법원 나의사건검색(ssgo.scourt.go.kr) 진행현황 자동 수집.

흐름(사건별 반복):
  1. Key Vault/env에서 사건목록(COURT-CASES) + Anthropic 키 로드
  2. 법원/년도/사건구분/일련번호/당사자명 입력
  3. 자동입력 방지문자(캡차)를 Anthropic vision으로 OCR (실패 시 새로고침 재시도)
  4. 검색 → 진행내용 탭 → 송달결과 '확인' 체크 → 일자별 진행사항 + 메타/기일 파싱
  5. 사건별 스냅샷을 호출자에게 반환 (webapp 전송은 main.py가 담당)

변동감지/알람은 fss-webapp 책임. 이 모듈은 '현재 스냅샷'만 정확히 만든다.
"""

import os
import re
import json
import time
import base64
import logging
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

URL = "https://ssgo.scourt.go.kr/ssgo/index.on"
PFX = "mf_ssgoTopMainTab_contents_content1_body_"

# 사건 단위 결과 상태
STATUS_OK = "success"
STATUS_NOT_FOUND = "not_found"        # 조회 결과 없음(사건번호/당사자 불일치 등)
STATUS_CAPTCHA_FAILED = "captcha_failed"
STATUS_CONFIG_MISSING = "config_missing"
STATUS_ERROR = "error"

MAX_CAPTCHA_TRY = int(os.getenv("CAPTCHA_MAX_TRY", "4"))
OCR_MODEL = os.getenv("CAPTCHA_OCR_MODEL", "claude-sonnet-4-6")


# ===== 자격증명/설정 로드 =====

def _kv_client():
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient
    vault_url = os.getenv("KEY_VAULT_URL", "").strip()
    return SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())


def resolve_anthropic_key():
    """Anthropic API 키: KEY_VAULT_URL 있으면 KV, 없으면 env.

    Returns: (key, error)
    """
    vault_url = os.getenv("KEY_VAULT_URL", "").strip()
    if vault_url:
        try:
            name = os.getenv("ANTHROPIC_API_KEY_SECRET_NAME", "ANTHROPIC-API-KEY")
            key = _kv_client().get_secret(name).value
            logger.info(f"Anthropic 키 로드: Key Vault ({name})")
            return key, None
        except Exception as e:
            return None, f"Key Vault에서 Anthropic 키 조회 실패: {e}"
    # env fallback (.env에 하이픈/언더스코어 둘 다 허용)
    key = (os.getenv("ANTHROPIC-API-KEY") or os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if key:
        logger.info("Anthropic 키 로드: 환경변수")
        return key, None
    return None, "Anthropic 키가 없습니다 (KEY_VAULT_URL 또는 ANTHROPIC_API_KEY)."


def resolve_court_cases():
    """추적 사건목록(JSON 배열): KEY_VAULT_URL 있으면 KV(COURT-CASES), 없으면 env(COURT_CASES).

    각 원소: {court, caseNo|case_no, party, id?, alias?, enabled?}
    Returns: (cases, error)  — cases는 정규화된 dict 리스트
    """
    vault_url = os.getenv("KEY_VAULT_URL", "").strip()
    raw = None
    if vault_url:
        try:
            name = os.getenv("COURT_CASES_SECRET_NAME", "COURT-CASES")
            raw = _kv_client().get_secret(name).value
            logger.info(f"사건목록 로드: Key Vault ({name})")
        except Exception as e:
            return None, f"Key Vault에서 사건목록 조회 실패: {e}"
    else:
        raw = os.getenv("COURT_CASES", "").strip()
        if raw:
            logger.info("사건목록 로드: 환경변수 COURT_CASES")

    if not raw:
        return None, "사건목록(COURT-CASES)이 비어있습니다."

    try:
        data = json.loads(raw)
    except Exception as e:
        return None, f"사건목록 JSON 파싱 실패: {e}"
    if not isinstance(data, list):
        return None, "사건목록은 JSON 배열이어야 합니다."

    cases = []
    for i, it in enumerate(data):
        if not isinstance(it, dict):
            logger.warning(f"사건목록[{i}] dict 아님 — 건너뜀")
            continue
        if it.get("enabled") is False:
            continue
        court = (it.get("court") or "").strip()
        case_no = (it.get("caseNo") or it.get("case_no") or "").strip()
        party = (it.get("party") or "").strip()
        if not (court and case_no and party):
            logger.warning(f"사건목록[{i}] 필수값 누락(court/caseNo/party) — 건너뜀")
            continue
        cases.append({
            "id": it.get("id") or it.get("alias") or case_no,
            "court": court, "caseNo": case_no, "party": party,
            "alias": it.get("alias"),
        })
    if not cases:
        return None, "유효한 사건이 없습니다 (필수값 확인)."
    return cases, None


# ===== 파싱 유틸 =====

def parse_case_no(case_no):
    m = re.match(r"^(\d{4})(\D+?)(\d+)$", case_no.strip())
    if not m:
        raise ValueError(f"사건번호 형식 오류: {case_no}")
    return m.group(1), m.group(2), m.group(3)  # year, 부호, serial


def _iso_date(s):
    """'2025.11.06' -> '2025-11-06'. 변환 불가하면 원본 반환."""
    s = (s or "").strip()
    m = re.match(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})$", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return s


def sel(name):
    return f"#{PFX}{name}"


# ===== 캡차 OCR =====

def ocr_captcha(png_bytes, api_key):
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    b64 = base64.standard_b64encode(png_bytes).decode()
    msg = client.messages.create(
        model=OCR_MODEL,
        max_tokens=20,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": "이 이미지는 가로줄이 그어진 6자리 숫자 캡차입니다. 숫자 6자리만 정확히 출력하세요. 다른 텍스트나 공백 없이 숫자만."},
            ],
        }],
    )
    txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return re.sub(r"\D", "", txt)


# ===== 결과 페이지 파싱 =====

def _check_delivery_confirm(page):
    """송달결과 '확인' 체크박스 ON (결과 칸이 'O시 도달' 등으로 채워짐)."""
    boxes = page.evaluate(r"""() => {
      const vis = el => { const r=el.getBoundingClientRect(); return r.width>0&&r.height>0; };
      const out=[];
      document.querySelectorAll('input[type=checkbox]').forEach(el=>{
        if(!vis(el)) return;
        let lab = el.getAttribute('aria-label')||el.getAttribute('title')||'';
        if(el.id){ const l=document.querySelector('label[for="'+el.id+'"]'); if(l) lab=lab||l.innerText; }
        out.push({id:el.id, label:(lab||'').trim(), checked:el.checked});
      });
      return out;
    }""")
    target = None
    for b in boxes:
        if ("송달" in b["label"] or "확인" in b["label"]) and not b["checked"]:
            target = b
            break
    if target is None:
        target = next((b for b in boxes if not b["checked"]), None)
    if target and target["id"]:
        try:
            page.check(f'#{target["id"]}', force=True)
            page.wait_for_timeout(1500)
            return True
        except Exception as e:
            logger.warning(f"송달결과 확인 체크 실패: {e}")
    return False


def _grids(page):
    """결과 페이지의 모든 표를 행렬로 추출."""
    return page.evaluate(r"""() => {
      const out=[];
      document.querySelectorAll('table').forEach(t=>{
        const rows=Array.from(t.querySelectorAll('tr')).map(tr=>
          Array.from(tr.querySelectorAll('th,td')).map(c=>c.innerText.replace(/\s+/g,' ').trim()));
        if(rows.length) out.push(rows);
      });
      return out;
    }""")


def _extract_progress_rows(grids):
    """헤더가 일자|내용|결과(|공시문)인 표 → [{date,content,result,notice}]."""
    best = None
    for rows in grids:
        hdr = rows[0] if rows else []
        if "일자" in hdr and "내용" in hdr and "결과" in hdr:
            if best is None or len(rows) > len(best):
                best = rows
    if not best:
        return []
    out = []
    for r in best[1:]:
        if not r or not r[0]:
            continue
        out.append({
            "date": _iso_date(r[0]),
            "content": r[1] if len(r) > 1 else "",
            "result": r[2] if len(r) > 2 else "",
            "notice": r[3] if len(r) > 3 else "",
        })
    return out


def _extract_hearings(grids):
    """헤더가 일자|시각|기일구분|기일장소|결과 인 표 → 기일 목록."""
    for rows in grids:
        hdr = rows[0] if rows else []
        if "일자" in hdr and "기일구분" in hdr:
            out = []
            for r in rows[1:]:
                if not r or not r[0]:
                    continue
                out.append({
                    "date": _iso_date(r[0]),
                    "time": r[1] if len(r) > 1 else "",
                    "type": r[2] if len(r) > 2 else "",
                    "location": r[3] if len(r) > 3 else "",
                    "result": r[4] if len(r) > 4 else "",
                })
            return out
    return []


def _extract_meta(page):
    """일반내용 기본정보(사건명/재판부/접수일/종국결과)를 key→value로."""
    pairs = page.evaluate(r"""() => {
      const res={};
      document.querySelectorAll('table').forEach(t=>{
        t.querySelectorAll('tr').forEach(tr=>{
          const cells=Array.from(tr.querySelectorAll('th,td')).map(c=>c.innerText.replace(/\s+/g,' ').trim());
          for(let i=0;i+1<cells.length;i+=2){ if(cells[i]&&!(cells[i] in res)) res[cells[i]]=cells[i+1]; }
        });
      });
      return res;
    }""")
    return {
        "case_name": pairs.get("사건명", ""),
        "court_dept": pairs.get("재판부", ""),
        "received_date": _iso_date(pairs.get("접수일", "")),
        "final_result": pairs.get("종국결과", ""),
    }


# ===== 단일 사건 검색 =====

def search_one_case(page, case, api_key):
    """단일 사건 검색 → 스냅샷 dict 반환."""
    court, case_no, party = case["court"], case["caseNo"], case["party"]
    result = {
        "case_id": case["id"], "court": court, "case_no": case_no, "party": party,
        "status": STATUS_ERROR, "error_message": None,
        "case_name": "", "court_dept": "", "received_date": "", "final_result": "",
        "progress": [], "hearings": [],
    }
    try:
        year, dvs, serial = parse_case_no(case_no)
    except ValueError as e:
        result["status"] = STATUS_ERROR
        result["error_message"] = str(e)
        return result

    dialog_msgs = []
    handler = lambda d: (dialog_msgs.append(d.message), d.accept())
    page.on("dialog", handler)
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3500)

        page.select_option(sel("sbx_cortCd"), label=court)
        page.wait_for_timeout(1200)
        page.select_option(sel("sbx_csYr"), label=year)
        opts = page.evaluate("(id)=>Array.from(document.getElementById(id).options).map(o=>o.text)",
                             f"{PFX}sbx_csDvsCd")
        if dvs not in opts:
            result["status"] = STATUS_ERROR
            result["error_message"] = f"사건구분 부호 '{dvs}' 가 {court} 옵션에 없음"
            return result
        page.select_option(sel("sbx_csDvsCd"), label=dvs)
        page.fill(sel("ibx_csSerial"), serial)
        page.fill(sel("ibx_btprNm"), party)

        searched = False
        for attempt in range(1, MAX_CAPTCHA_TRY + 1):
            cap = page.query_selector(sel("img_captcha"))
            answer = ocr_captcha(cap.screenshot(), api_key)
            logger.info(f"[{case_no}] 캡차 OCR 시도 {attempt}: '{answer}'")
            if len(answer) != 6:
                page.click(sel("btn_reloadCaptcha"))
                page.wait_for_timeout(1500)
                continue
            page.fill(sel("ibx_answer"), answer)
            page.wait_for_timeout(300)
            dialog_msgs.clear()
            page.click(sel("btn_srchCs"))
            page.wait_for_timeout(5000)

            joined = " ".join(dialog_msgs)
            if "방지문자" in joined or "다시" in joined and "입력" in joined:
                logger.info(f"[{case_no}] 캡차 거부: {joined[:100]} → 재시도")
                page.wait_for_timeout(600)
                continue
            # 조회 결과 없음 안내
            if "조회된 결과가 없" in joined or "일치하는" in joined:
                result["status"] = STATUS_NOT_FOUND
                result["error_message"] = joined[:200]
                return result
            searched = True
            break

        if not searched:
            result["status"] = STATUS_CAPTCHA_FAILED
            result["error_message"] = f"캡차 {MAX_CAPTCHA_TRY}회 실패"
            return result

        page.wait_for_timeout(1500)
        # 진행내용 탭 + 송달확인
        for label in ["진행내용", "진행상황"]:
            try:
                tab = page.get_by_text(label, exact=True).first
                if tab.is_visible(timeout=1500):
                    tab.click()
                    page.wait_for_timeout(1800)
                    break
            except Exception:
                continue
        _check_delivery_confirm(page)

        grids = _grids(page)
        result.update(_extract_meta(page))
        result["progress"] = _extract_progress_rows(grids)
        result["hearings"] = _extract_hearings(grids)

        if not result["progress"] and not result["case_name"]:
            result["status"] = STATUS_NOT_FOUND
            result["error_message"] = "진행내용/사건정보를 찾지 못함"
        else:
            result["status"] = STATUS_OK
        return result

    except PlaywrightTimeout as e:
        result["status"] = STATUS_ERROR
        result["error_message"] = f"타임아웃: {e}"
        return result
    except Exception as e:
        result["status"] = STATUS_ERROR
        result["error_message"] = str(e)
        return result
    finally:
        try:
            page.remove_listener("dialog", handler)
        except Exception:
            pass


def run_crawl():
    """전체 사건 수집.

    Returns: dict {"status": ..., "error": ..., "results": [snapshot,...], "crawled_at": iso}
    """
    api_key, kerr = resolve_anthropic_key()
    if kerr:
        return {"status": STATUS_CONFIG_MISSING, "error": kerr, "results": [], "crawled_at": None}
    cases, cerr = resolve_court_cases()
    if cerr:
        return {"status": STATUS_CONFIG_MISSING, "error": cerr, "results": [], "crawled_at": None}

    crawled_at = datetime.now(KST).isoformat()
    logger.info(f"=== 나의사건검색 수집 시작: {len(cases)}건 ===")

    results = []
    with sync_playwright() as p:
        is_ci = os.getenv("CI", "false").lower() == "true"
        browser = p.chromium.launch(headless=is_ci, args=[] if is_ci else ["--start-maximized"])
        ctx = browser.new_context(viewport={"width": 1400, "height": 1200}, locale="ko-KR")
        try:
            for case in cases:
                page = ctx.new_page()
                try:
                    snap = search_one_case(page, case, api_key)
                    snap["crawled_at"] = crawled_at
                    results.append(snap)
                    logger.info(f"[{case['caseNo']}] status={snap['status']} "
                                f"progress={len(snap['progress'])}건")
                finally:
                    page.close()
        finally:
            browser.close()

    ok = sum(1 for r in results if r["status"] == STATUS_OK)
    status = STATUS_OK if ok == len(results) else ("partial" if ok else STATUS_ERROR)
    return {"status": status, "error": None, "results": results, "crawled_at": crawled_at}


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    out = run_crawl()
    print(json.dumps(out, ensure_ascii=False, indent=2))
