"""Public-source adapters for announcements, ETF data, and index constituents."""

from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


USER_AGENT = "invest-public-source-collector/1.0"
CNINFO_MAP = "https://www.cninfo.com.cn/new/data/szse_stock.json"
CNINFO_QUERY = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_PDF = "https://static.cninfo.com.cn/"
SZSE_REPORT = "https://www.szse.cn/api/report/ShowReport/data"


def _request(url: str, data: dict[str, str] | None = None) -> tuple[dict[str, str], bytes]:
    # Request a public endpoint with bounded timeout and no hidden browser state.
    payload = urllib.parse.urlencode(data).encode("utf-8") if data else None
    headers = {"User-Agent": USER_AGENT}
    if data:
        headers.update({
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        })
    request = urllib.request.Request(url, data=payload, headers=headers)
    with urllib.request.urlopen(request, timeout=25) as response:
        return dict(response.headers.items()), response.read()


def _json(url: str, data: dict[str, str] | None = None) -> Any:
    # Decode a JSON or JSONP response while preserving source bytes separately upstream.
    _, body = _request(url, data)
    text = _decode(body).strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    return json.loads(text)


def _decode(body: bytes) -> str:
    # Prefer UTF-8 but recover common mainland legacy encodings without silent replacement.
    text = body.decode("utf-8-sig", errors="replace")
    if "�" in text or any(marker in text for marker in ("����", "ƽ��")):
        try:
            return body.decode("gb18030")
        except UnicodeDecodeError:
            pass
    return text


def _safe_name(value: str) -> str:
    # Keep evidence filenames portable and bounded.
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value)


def _write_json(folder: Path, name: str, payload: Any) -> str:
    # Write one UTF-8 evidence object and return its relative filename.
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{_safe_name(name)}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target.name


def _security_map() -> dict[str, dict[str, str]]:
    # Read the CNINFO code-to-organization map required by announcement queries.
    data = _json(CNINFO_MAP)
    rows = data.get("stockList", []) if isinstance(data, dict) else []
    return {str(row.get("code")): row for row in rows if row.get("code")}


def fetch_cninfo_announcements(
    stock_code: str,
    start: str,
    end: str,
    page_size: int = 30,
) -> dict[str, Any]:
    # Fetch paginated announcement metadata for one security without downloading files.
    mapping = _security_map()
    item = mapping.get(stock_code.zfill(6))
    if not item or not item.get("orgId"):
        raise ValueError(f"巨潮证券映射中不存在 {stock_code}")
    data = {
        "pageNum": "1", "pageSize": str(min(max(page_size, 1), 30)), "column": "szse",
        "tabName": "fulltext", "plate": "sz;sh;bj",
        "stock": f"{stock_code.zfill(6)},{item['orgId']}", "seDate": f"{start}~{end}",
        "isHLtitle": "true",
    }
    result = _json(CNINFO_QUERY, data)
    return {
        "source": "cninfo",
        "stock_code": stock_code.zfill(6),
        "org_id": item["orgId"],
        "start": start,
        "end": end,
        "total": result.get("totalAnnouncement", 0),
        "announcements": result.get("announcements") or [],
    }


def download_cninfo_pdfs(
    announcements: list[dict[str, Any]],
    folder: Path,
) -> list[dict[str, Any]]:
    # Download only explicit announcement attachments and record hashes for provenance.
    outputs = []
    pdf_folder = folder / "pdf"
    pdf_folder.mkdir(parents=True, exist_ok=True)
    for item in announcements:
        relative = str(item.get("adjunctUrl") or "")
        if not relative:
            continue
        url = urllib.parse.urljoin(CNINFO_PDF, relative)
        try:
            headers, body = _request(url)
            target = pdf_folder / f"{_safe_name(item.get('announcementId', 'unknown'))}.pdf"
            target.write_bytes(body)
            outputs.append({
                "announcement_id": item.get("announcementId"), "url": url,
                "path": str(target), "sha256": hashlib.sha256(body).hexdigest(),
                "content_type": headers.get("Content-Type", ""), "bytes": len(body),
            })
        except Exception as exc:
            outputs.append({"announcement_id": item.get("announcementId"), "url": url,
                            "error": f"{type(exc).__name__}: {exc}"})
    return outputs


def fetch_szse_etf_size(code: str, start: str, end: str) -> dict[str, Any]:
    # Fetch Shenzhen ETF shares/size records from the public report endpoint.
    query = urllib.parse.urlencode({
        "SHOWTYPE": "JSON", "CATALOGID": "scsj_fund_jjgm", "TABKEY": "tab1",
        "jjlb": "ETF", "txtDm": code, "txtStart": start, "txtEnd": end,
        "PAGENO": "1",
    })
    result = _json(f"{SZSE_REPORT}?{query}")
    page = result[0] if isinstance(result, list) and result else {}
    return {"source": "szse", "domain": "etf_size", "code": code,
            "start": start, "end": end, "metadata": page.get("metadata", {}),
            "rows": page.get("data") or []}


def fetch_csindex_constituents(index_code: str, folder: Path) -> dict[str, Any]:
    # Download a public CSI constituent workbook and retain its content hash.
    url = ("https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
           f"file/autofile/cons/{index_code}cons.xls")
    _, body = _request(url)
    target = folder / f"{_safe_name(index_code)}cons.xls"
    folder.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return {"source": "csindex", "index_code": index_code, "url": url,
            "path": str(target), "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body)}


def collect(
    output: Path,
    stock: str | None = None,
    etf: str | None = None,
    index: str | None = None,
    start: str = "2026-09-01",
    end: str = "2026-09-20",
    download_pdfs: bool = False,
) -> dict[str, Any]:
    # Run selected source collectors and write a manifest for reproducibility.
    run = output / datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    manifest: dict[str, Any] = {"started_at": datetime.now().astimezone().isoformat(),
                                "output": str(run), "sources": [], "errors": []}
    if stock:
        try:
            result = fetch_cninfo_announcements(stock, start, end)
            _write_json(run, f"cninfo_{stock}_{start}_{end}", result)
            if download_pdfs:
                result["pdfs"] = download_cninfo_pdfs(result["announcements"], run)
            manifest["sources"].append(result)
        except Exception as exc:
            manifest["errors"].append({"source": "cninfo", "error": str(exc)})
    if etf:
        try:
            result = fetch_szse_etf_size(etf, start, end)
            _write_json(run, f"szse_etf_size_{etf}_{start}_{end}", result)
            manifest["sources"].append(result)
        except Exception as exc:
            manifest["errors"].append({"source": "szse", "error": str(exc)})
    if index:
        try:
            result = fetch_csindex_constituents(index, run / "index")
            manifest["sources"].append(result)
        except Exception as exc:
            manifest["errors"].append({"source": "csindex", "error": str(exc)})
    manifest["finished_at"] = datetime.now().astimezone().isoformat()
    _write_json(run, "manifest", manifest)
    return manifest
