#!/usr/bin/env python3
"""학교업무 도움자료 — 자료 폴더별 파일 이름 목록 수집기

index.html의 자료 목록(항목 ID·웹하드 링크)을 읽어, 각 웹하드 공개 공유 폴더 안의
파일·하위 폴더 이름을 모아 data 폴더에 저장한다. (사이트 검색창의 '파일 이름 검색'용)

  python3 collector/collect.py --page index.html --data data --fallback files.js
      수집 실행. data/files.json(최신 목록), data/changes.json(최근 90일 변동),
      data/status.json(마지막 실행 결과)을 갱신한다.
  python3 collector/collect.py --build-js data/files.json _site/files.js
      files.json → 페이지가 불러오는 files.js 만들기.

안전장치
  - 실패한 폴더는 이전 목록을 그대로 유지한다.
  - 실패한 폴더가 전체의 20% 이상이면 목록을 바꾸지 않고 종료 코드 1로 끝낸다.

파이썬 표준 라이브러리만 사용한다.
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
SHARE_API = "https://api.drive.worksmobile.com/rl/24101/v1/shared-links"  # 경남교육청 웹하드 공개 공유
UA = "Mozilla/5.0 (compatible; gne-work-help-collector/1.0)"
SUBFOLDER_DEPTH = 4       # 하위 폴더를 몇 단계까지 들어갈지
PAGE_SIZE = 500
PAUSE = 0.15              # 요청 사이 쉬는 시간(초)
FAIL_LIMIT = 0.2          # 이 비율 이상 실패하면 목록을 바꾸지 않음
KEEP_DAYS = 90            # 변동 기록 보관 기간

JS_HEAD = (
    "// 학교업무 도움자료 — 자료 폴더별 파일 이름 목록 (사이트 검색용)\n"
    "// collector/collect.py가 자동으로 만듭니다. 직접 고치지 마세요.\n"
)


class FetchError(Exception):
    pass


# ── 자료 목록 읽기 ──────────────────────────────────────────────
def read_items(page: Path):
    """index.html에서 (항목 ID, 웹하드 링크)를 순서대로 꺼낸다."""
    html = page.read_text(encoding="utf-8")
    pairs = re.findall(r'\{\s*id:\s*"([^"]+)"[^{}]*?url:\s*"([^"]+)"', html)
    seen, items = set(), []
    for item_id, url in pairs:
        if item_id in seen or "works.do" not in url:
            continue
        seen.add(item_id)
        items.append((item_id, url))
    if not items:
        raise SystemExit("index.html에서 자료 목록을 찾지 못했습니다.")
    return items


# ── 파일 입출력 ─────────────────────────────────────────────────
def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def load_files_js(path: Path):
    """files.js(또는 예전 data-files.js 형식)에서 목록 객체만 꺼낸다."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return {}


def write_json(path: Path, obj, pretty=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if pretty:
        body = json.dumps(obj, ensure_ascii=False, indent=1)
    else:
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    path.write_text(body + "\n", encoding="utf-8")


def build_js(files_json: Path, out_js: Path):
    data = load_json(files_json, None)
    if not isinstance(data, dict):
        raise SystemExit(f"{files_json}을 읽지 못했습니다.")
    out_js.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    out_js.write_text(JS_HEAD + "const filesData = " + body + ";\n", encoding="utf-8")
    print(f"{out_js} 작성 (폴더 {len(data)}개, 파일 이름 {sum(len(v) for v in data.values())}개)")


# ── 웹하드 요청 ─────────────────────────────────────────────────
def http_get(url: str, want_json=True):
    last = None
    for attempt in range(3):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, text/html"})
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                if not want_json:
                    return res.geturl()
                return json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code == 429:
                time.sleep(4)
                continue
            if 400 <= e.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
            last = type(e).__name__
        time.sleep(1.5 * (attempt + 1))
    raise FetchError(last or "알 수 없는 오류")


def share_token(short_url: str) -> str:
    final = http_get(short_url, want_json=False)
    m = re.search(r"pubDLink/([^/?#]+)", final)
    if not m:
        raise FetchError("공유 링크 주소 형식이 다름")
    return m.group(1)


def is_folder(entry: dict) -> bool:
    return str(entry.get("fileType", "")).upper() == "FOLDER" or (entry.get("childFolderCount") or 0) > 0


def folder_names(token: str, folder_id: str, depth: int, out: list):
    """폴더 안 이름을 이름순으로 담고, 하위 폴더는 그 자리에서 이어서 들어간다."""
    cursor = None
    while True:
        query = {
            "service": "drive_link",
            "parentFileId": folder_id,
            "orderBy": "fileName asc",
            "count": PAGE_SIZE,
        }
        if cursor:
            query["cursor"] = cursor
        page = http_get(f"{SHARE_API}/{token}/files?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote))
        for entry in page.get("files") or []:
            name = entry.get("fileName")
            if not name:
                continue
            out.append(name)
            if is_folder(entry) and depth < SUBFOLDER_DEPTH and entry.get("fileId"):
                time.sleep(PAUSE)
                folder_names(token, entry["fileId"], depth + 1, out)
        cursor = (page.get("responseMetaData") or {}).get("nextCursor")
        if not cursor:
            return
        time.sleep(PAUSE)


def collect_one(short_url: str) -> list:
    token = share_token(short_url)
    time.sleep(PAUSE)
    info = http_get(f"{SHARE_API}/{token}?service=drive_link")
    root = info.get("rootFileId")
    if not root:
        raise FetchError("폴더 정보 없음")
    names = []
    time.sleep(PAUSE)
    folder_names(token, root, 0, names)
    return names


# ── 변동 비교 ───────────────────────────────────────────────────
def diff(old: dict, new: dict) -> dict:
    changes = {}
    for item_id in sorted(set(old) | set(new)):
        before, after = old.get(item_id, []), new.get(item_id, [])
        if before == after:
            continue
        b, a = set(before), set(after)
        entry = {}
        if a - b:
            entry["추가"] = sorted(a - b)
        if b - a:
            entry["삭제"] = sorted(b - a)
        if not entry:
            entry["순서변경"] = True
        changes[item_id] = entry
    return changes


# ── 수집 실행 ───────────────────────────────────────────────────
def run(page: Path, data_dir: Path, fallback: Path, limit: int = 0) -> int:
    started = datetime.now(KST)
    items = read_items(page)
    if limit:
        items = items[:limit]

    previous = load_json(data_dir / "files.json", None)
    if not isinstance(previous, dict):
        previous = load_files_js(fallback)
        print(f"이전 수집 결과가 없어 {fallback}의 목록({len(previous)}개 폴더)을 기준으로 삼습니다.")

    fresh, failed = {}, []
    for n, (item_id, url) in enumerate(items, 1):
        try:
            fresh[item_id] = collect_one(url)
        except FetchError as e:
            failed.append({"id": item_id, "이유": str(e), "이전목록유지": item_id in previous})
            if item_id in previous:
                fresh[item_id] = previous[item_id]
        if n % 25 == 0:
            print(f"  … {n}/{len(items)} 폴더 (실패 {len(failed)})", flush=True)
        time.sleep(PAUSE)

    fail_rate = len(failed) / len(items)
    aborted = fail_rate >= FAIL_LIMIT
    changes = {} if aborted else diff(previous, fresh)

    status = {
        "실행시각": started.isoformat(timespec="seconds"),
        "소요초": round((datetime.now(KST) - started).total_seconds()),
        "폴더수": len(items),
        "성공": len(items) - len(failed),
        "실패": len(failed),
        "반영": not aborted,
        "바뀐폴더": len(changes),
        "파일이름수": sum(len(v) for v in (previous if aborted else fresh).values()),
        "실패목록": failed,
    }

    if not aborted:
        write_json(data_dir / "files.json", fresh)
        log = load_json(data_dir / "changes.json", [])
        if changes:
            log.append({"날짜": started.strftime("%Y-%m-%d"), "변동": changes})
        cutoff = (started - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
        log = [e for e in log if e.get("날짜", "") >= cutoff]
        write_json(data_dir / "changes.json", log, pretty=True)
    elif not (data_dir / "files.json").exists() and previous:
        write_json(data_dir / "files.json", previous)
    write_json(data_dir / "status.json", status, pretty=True)

    print(f"\n폴더 {len(items)}개 중 {status['성공']}개 성공, {len(failed)}개 실패")
    if failed:
        for f in failed[:30]:
            print(f"  - {f['id']}: {f['이유']}" + (" (이전 목록 유지)" if f["이전목록유지"] else ""))
    if aborted:
        print(f"실패가 {fail_rate:.0%}로 기준({FAIL_LIMIT:.0%}) 이상이라 목록을 바꾸지 않았습니다.")
        return 1
    added = sum(len(c.get("추가", [])) for c in changes.values())
    removed = sum(len(c.get("삭제", [])) for c in changes.values())
    print(f"바뀐 폴더 {len(changes)}개 (새 이름 {added}개, 사라진 이름 {removed}개), 전체 파일 이름 {status['파일이름수']}개")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--page", type=Path, default=Path("index.html"))
    ap.add_argument("--data", type=Path, default=Path("data"))
    ap.add_argument("--fallback", type=Path, default=Path("files.js"))
    ap.add_argument("--limit", type=int, default=0, help="앞에서부터 N개 폴더만 (시험용)")
    ap.add_argument("--build-js", nargs=2, type=Path, metavar=("FILES_JSON", "OUT_JS"))
    args = ap.parse_args()
    if args.build_js:
        build_js(*args.build_js)
        return 0
    return run(args.page, args.data, args.fallback, args.limit)


if __name__ == "__main__":
    sys.exit(main())
