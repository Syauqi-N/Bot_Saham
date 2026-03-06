import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag


@dataclass
class LogbookConfig:
    cas_login_url: str
    form_url: str
    username: str
    password: str
    timeout_connect: int = 10
    timeout_read: int = 45


@dataclass
class LogbookEntry:
    date: str
    start_time: str
    end_time: str
    activity: str
    related: bool = True
    course_keyword: str = ""
    agree: bool = True


@dataclass
class ParsedLogbookForm:
    action_url: str
    payload: Dict[str, str]
    date_field: Optional[str]
    start_time_field: Optional[str]
    end_time_field: Optional[str]
    activity_field: Optional[str]
    related_radio_name: Optional[str]
    related_yes_value: Optional[str]
    related_no_value: Optional[str]
    course_field: Optional[str]
    course_options: List[Tuple[str, str]]
    agree_field: Optional[str]
    agree_value: Optional[str]


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize(value: str) -> str:
    return _clean_text(value).lower()


def _contains_any(text: str, keywords: List[str]) -> bool:
    normalized = _normalize(text)
    return any(keyword in normalized for keyword in keywords)


def _control_context(tag: Tag) -> str:
    contexts: List[str] = []
    name = str(tag.get("name") or "").strip()
    control_id = str(tag.get("id") or "").strip()
    placeholder = str(tag.get("placeholder") or "").strip()
    title = str(tag.get("title") or "").strip()

    for part in [name, control_id, placeholder, title]:
        if part:
            contexts.append(part)

    row = tag.find_parent("tr")
    if row:
        contexts.append(row.get_text(" ", strip=True))
    parent = tag.find_parent()
    if parent:
        contexts.append(parent.get_text(" ", strip=True))

    return _normalize(" ".join(contexts))


def _score_candidate(name: str, context: str, keywords: List[str]) -> int:
    score = 0
    lname = _normalize(name)
    lcontext = _normalize(context)
    for keyword in keywords:
        if keyword in lname:
            score += 5
        if keyword in lcontext:
            score += 2
    return score


def _extract_default_payload_from_scope(scope: Any) -> Dict[str, str]:
    payload: Dict[str, str] = {}

    for input_tag in scope.find_all("input"):
        if not isinstance(input_tag, Tag):
            continue
        name = str(input_tag.get("name") or "").strip()
        if not name:
            continue
        input_type = _normalize(str(input_tag.get("type") or "text"))
        value = str(input_tag.get("value") or "")

        if input_type in {"button", "reset", "image", "file"}:
            continue
        if input_type == "radio":
            if input_tag.has_attr("checked"):
                payload[name] = value or "on"
            continue
        if input_type == "checkbox":
            if input_tag.has_attr("checked"):
                payload[name] = value or "on"
            continue
        payload[name] = value

    for textarea in scope.find_all("textarea"):
        if not isinstance(textarea, Tag):
            continue
        name = str(textarea.get("name") or "").strip()
        if not name:
            continue
        payload[name] = textarea.get_text("", strip=False) or ""

    for select in scope.find_all("select"):
        if not isinstance(select, Tag):
            continue
        name = str(select.get("name") or "").strip()
        if not name:
            continue
        options = [option for option in select.find_all("option") if isinstance(option, Tag)]
        if not options:
            payload[name] = ""
            continue
        selected = next((option for option in options if option.has_attr("selected")), options[0])
        payload[name] = str(selected.get("value") or "")

    return payload


def _extract_default_payload(form: Tag) -> Dict[str, str]:
    return _extract_default_payload_from_scope(form)


def parse_cas_login_form(html: str, page_url: str) -> Tuple[Optional[str], Optional[Dict[str, str]], Optional[str]]:
    soup = BeautifulSoup(html or "", "html.parser")
    form = soup.find("form", {"id": "fm1"})
    if not isinstance(form, Tag):
        form = soup.find("form")
    if not isinstance(form, Tag):
        return None, None, "Form login CAS tidak ditemukan."

    action = str(form.get("action") or "").strip()
    action_url = urljoin(page_url, action) if action else page_url
    payload = _extract_default_payload(form)

    username_field = None
    password_field = None
    for input_tag in form.find_all("input"):
        if not isinstance(input_tag, Tag):
            continue
        name = str(input_tag.get("name") or "").strip()
        if not name:
            continue
        input_type = _normalize(str(input_tag.get("type") or "text"))
        if input_type == "password":
            password_field = name
        context = _control_context(input_tag)
        if "username" in name.lower() or "netid" in context or "username" in context:
            username_field = name
        if "password" in name.lower():
            password_field = name

    if not username_field:
        username_field = "username"
    if not password_field:
        password_field = "password"

    payload.setdefault("_eventId", "submit")
    payload[username_field] = ""
    payload[password_field] = ""
    return action_url, payload, None


def _pick_logbook_form(soup: BeautifulSoup) -> Optional[Tag]:
    forms = [form for form in soup.find_all("form") if isinstance(form, Tag)]
    if not forms:
        return None

    best_form = forms[0]
    best_score = -1
    for form in forms:
        text = _normalize(form.get_text(" ", strip=True))
        score = 0
        if form.find("textarea"):
            score += 4
        for keyword in ["logbook", "kegiatan", "materi", "jam mulai", "jam selesai", "simpan"]:
            if keyword in text:
                score += 2
        if form.find("input", {"type": "submit"}):
            score += 1
        if score > best_score:
            best_score = score
            best_form = form
    return best_form


def _visible_text_like_controls(controls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        control
        for control in controls
        if control["type"] in {"text", "time", "date", "datetime-local", "number"}
        and not _contains_any(str(control["context"]), ["token", "csrf", "session", "captcha"])
    ]


def _collect_controls(scope: Any) -> List[Dict[str, Any]]:
    controls: List[Dict[str, Any]] = []

    for tag in scope.find_all(["input", "textarea", "select"]):
        if not isinstance(tag, Tag):
            continue
        name = str(tag.get("name") or "").strip()
        if not name:
            continue

        if tag.name == "textarea":
            control_type = "textarea"
            value = tag.get_text("", strip=False) or ""
        elif tag.name == "select":
            control_type = "select"
            value = ""
        else:
            control_type = _normalize(str(tag.get("type") or "text"))
            value = str(tag.get("value") or "")

        controls.append(
            {
                "name": name,
                "type": control_type,
                "value": value,
                "context": _control_context(tag),
                "tag": tag,
            }
        )
    return controls


def _pick_field_name(controls: List[Dict[str, Any]], allowed_types: List[str], keywords: List[str]) -> Optional[str]:
    best_name = None
    best_score = 0
    for control in controls:
        if control["type"] not in allowed_types:
            continue
        score = _score_candidate(control["name"], str(control["context"]), keywords)
        if score > best_score:
            best_score = score
            best_name = str(control["name"])
    return best_name if best_score > 0 else None


def _radio_option_label(tag: Tag) -> str:
    pieces: List[str] = []
    for sibling in tag.next_siblings:
        text = getattr(sibling, "get_text", None)
        if callable(text):
            pieces.append(text(" ", strip=True))
        else:
            pieces.append(str(sibling).strip())
        if len(" ".join(pieces)) >= 20:
            break
    if not pieces:
        parent = tag.find_parent()
        if parent:
            pieces.append(parent.get_text(" ", strip=True))
    return _normalize(" ".join(pieces))


def _pick_related_radio(controls: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for control in controls:
        if control["type"] != "radio":
            continue
        groups.setdefault(str(control["name"]), []).append(control)

    if not groups:
        return None, None, None

    best_group_name = None
    best_score = 0
    for name, members in groups.items():
        context = " ".join([name] + [str(member["context"]) for member in members])
        score = _score_candidate(
            name,
            context,
            ["sesuai", "mata kuliah", "matakuliah", "matkul", "diajarkan", "related"],
        )
        if score > best_score:
            best_score = score
            best_group_name = name

    if not best_group_name:
        if len(groups) == 1:
            best_group_name = next(iter(groups))
        else:
            return None, None, None

    yes_value = None
    no_value = None
    members = groups[best_group_name]
    for member in members:
        value = str(member["value"] or "on")
        tag = member["tag"]
        label = _radio_option_label(tag)
        token = _normalize(f"{value} {label}")
        if any(item in token for item in [" ya", " yes", "true", "1", "setuju"]):
            yes_value = value
        if any(item in token for item in [" tidak", " no", "false", "0"]):
            no_value = value

    if yes_value is None and members:
        yes_value = str(members[0]["value"] or "on")
    if no_value is None and len(members) >= 2:
        no_value = str(members[-1]["value"] or "on")

    return best_group_name, yes_value, no_value


def _pick_course_select(controls: List[Dict[str, Any]]) -> Tuple[Optional[str], List[Tuple[str, str]]]:
    best_name = None
    best_score = 0
    best_options: List[Tuple[str, str]] = []

    for control in controls:
        if control["type"] != "select":
            continue
        tag = control["tag"]
        options = [
            (str(option.get("value") or "").strip(), _clean_text(option.get_text(" ", strip=True)))
            for option in tag.find_all("option")
            if isinstance(option, Tag)
        ]
        context = str(control["context"])
        score = _score_candidate(
            str(control["name"]),
            context,
            ["matakuliah", "mata kuliah", "matkul", "kuliah", "course", "mk"],
        )
        if options and any("ri0" in _normalize(text) or "ri0" in _normalize(value) for value, text in options):
            score += 2
        if score > best_score:
            best_score = score
            best_name = str(control["name"])
            best_options = options

    if best_score <= 0:
        return None, []
    return best_name, best_options


def _pick_agree_checkbox(controls: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    checkboxes = [control for control in controls if control["type"] == "checkbox"]
    if not checkboxes:
        return None, None

    best_control = None
    best_score = 0
    for checkbox in checkboxes:
        score = _score_candidate(
            str(checkbox["name"]),
            str(checkbox["context"]),
            ["saya menyatakan", "pernyataan", "benar", "setuju", "agree", "cek"],
        )
        if score > best_score:
            best_score = score
            best_control = checkbox

    if not best_control:
        best_control = checkboxes[0]

    value = str(best_control["value"] or "on")
    return str(best_control["name"]), value


def _summarize_form_candidates(forms: List[Tag]) -> str:
    parts: List[str] = []
    for index, form in enumerate(forms[:5], start=1):
        action = _clean_text(str(form.get("action") or "")) or "-"
        controls_count = len(_collect_controls(form))
        text = _normalize(form.get_text(" ", strip=True))
        hints = 0
        for keyword in ["logbook", "kegiatan", "materi", "jam mulai", "jam selesai", "simpan"]:
            if keyword in text:
                hints += 1
        parts.append(f"#{index}(action={action}, controls={controls_count}, hints={hints})")
    return "; ".join(parts) if parts else "(tidak ada form)"


def _page_title(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    title = soup.find("title")
    if isinstance(title, Tag):
        return _clean_text(title.get_text(" ", strip=True))
    return "-"


def parse_logbook_form(html: str, page_url: str) -> Tuple[Optional[ParsedLogbookForm], Optional[str]]:
    def parse_candidate(action_url: str, payload: Dict[str, str], controls: List[Dict[str, Any]]) -> Tuple[ParsedLogbookForm, int]:

        date_field = _pick_field_name(controls, ["text", "date", "hidden"], ["tanggal", "tgl", "date"])
        start_time_field = _pick_field_name(
            controls,
            ["text", "time"],
            ["jam mulai", "jammulai", "jam_mulai", "mulai", "start"],
        )
        end_time_field = _pick_field_name(
            controls,
            ["text", "time"],
            ["jam selesai", "jamselesai", "jam_selesai", "selesai", "end"],
        )
        activity_field = _pick_field_name(
            controls,
            ["textarea", "text"],
            ["kegiatan", "materi", "aktivitas", "activity", "uraian", "deskripsi"],
        )

        # Fallback for older MIS forms: identify fields by control order.
        text_like = _visible_text_like_controls(controls)
        used_names = {name for name in [date_field, start_time_field, end_time_field] if name}
        if not date_field and text_like:
            date_field = str(text_like[0]["name"])
            used_names.add(date_field)
        if not start_time_field:
            for control in text_like:
                name = str(control["name"])
                if name not in used_names:
                    start_time_field = name
                    used_names.add(name)
                    break
        if not end_time_field:
            for control in text_like:
                name = str(control["name"])
                if name not in used_names:
                    end_time_field = name
                    used_names.add(name)
                    break
        if not activity_field:
            first_textarea = next((c for c in controls if c["type"] == "textarea"), None)
            if first_textarea:
                activity_field = str(first_textarea["name"])

        related_radio_name, related_yes_value, related_no_value = _pick_related_radio(controls)
        course_field, course_options = _pick_course_select(controls)
        agree_field, agree_value = _pick_agree_checkbox(controls)

        candidate = ParsedLogbookForm(
            action_url=action_url,
            payload=payload,
            date_field=date_field,
            start_time_field=start_time_field,
            end_time_field=end_time_field,
            activity_field=activity_field,
            related_radio_name=related_radio_name,
            related_yes_value=related_yes_value,
            related_no_value=related_no_value,
            course_field=course_field,
            course_options=course_options,
            agree_field=agree_field,
            agree_value=agree_value,
        )
        score = 0
        if date_field:
            score += 3
        if start_time_field:
            score += 3
        if end_time_field:
            score += 3
        if activity_field:
            score += 4
        if related_radio_name:
            score += 1
        if course_field:
            score += 1
        if agree_field:
            score += 1
        # Extra confidence when context explicitly mentions logbook keywords.
        context_blob = " ".join([str(control["context"]) for control in controls])
        score += _score_candidate("scope", context_blob, ["logbook", "kegiatan", "materi", "jam mulai", "jam selesai"]) // 6
        return candidate, score

    soup = BeautifulSoup(html or "", "html.parser")
    forms = [form for form in soup.find_all("form") if isinstance(form, Tag)]

    # Prioritize the old selector first, but still evaluate all forms.
    preferred = _pick_logbook_form(soup)
    ordered_forms = []
    if preferred:
        ordered_forms.append(preferred)
    for form in forms:
        if form is not preferred:
            ordered_forms.append(form)

    best_candidate = None
    best_score = -1
    for form in ordered_forms:
        action = str(form.get("action") or "").strip()
        action_url = urljoin(page_url, action) if action else page_url
        payload = _extract_default_payload(form)
        controls = _collect_controls(form)
        candidate, score = parse_candidate(action_url, payload, controls)
        if score > best_score:
            best_score = score
            best_candidate = candidate
        if score >= 13:
            break

    # Fallback: some legacy pages may render controls outside <form>.
    doc_payload = _extract_default_payload_from_scope(soup)
    doc_controls = _collect_controls(soup)
    doc_candidate, doc_score = parse_candidate(page_url, doc_payload, doc_controls)
    if doc_score > best_score:
        best_score = doc_score
        best_candidate = doc_candidate

    if not best_candidate:
        return None, "Form logbook tidak ditemukan."
    if best_score < 8:
        title = _page_title(html)
        return (
            None,
            "Gagal identifikasi field form logbook (score={score}). title={title}. Kandidat form: {summary}".format(
                score=best_score,
                title=title,
                summary=_summarize_form_candidates(forms),
            ),
        )

    return best_candidate, None


def _pick_course_value(options: List[Tuple[str, str]], keyword: str) -> Optional[str]:
    target = _normalize(keyword)
    if not target:
        return None
    for value, text in options:
        token = _normalize(f"{value} {text}")
        if target in token:
            return value
    return None


def build_logbook_payload(parsed_form: ParsedLogbookForm, entry: LogbookEntry) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    payload = dict(parsed_form.payload)
    activity = _clean_text(entry.activity)
    if not activity:
        return None, "Kegiatan/materi tidak boleh kosong."

    missing_fields: List[str] = []
    if not parsed_form.date_field:
        missing_fields.append("tanggal")
    if not parsed_form.start_time_field:
        missing_fields.append("jam mulai")
    if not parsed_form.end_time_field:
        missing_fields.append("jam selesai")
    if not parsed_form.activity_field:
        missing_fields.append("kegiatan/materi")
    if missing_fields:
        known = sorted(parsed_form.payload.keys())
        known_preview = ", ".join(known[:20]) if known else "(tidak ada key payload)"
        return (
            None,
            "Field form MIS tidak dikenali: {missing}. Key payload terdeteksi: {known}.".format(
                missing=", ".join(missing_fields),
                known=known_preview,
            ),
        )

    payload[parsed_form.date_field] = entry.date
    payload[parsed_form.start_time_field] = entry.start_time
    payload[parsed_form.end_time_field] = entry.end_time
    payload[parsed_form.activity_field] = activity

    if parsed_form.related_radio_name:
        if entry.related and parsed_form.related_yes_value is not None:
            payload[parsed_form.related_radio_name] = parsed_form.related_yes_value
        elif not entry.related and parsed_form.related_no_value is not None:
            payload[parsed_form.related_radio_name] = parsed_form.related_no_value

    if entry.related:
        if not parsed_form.course_field:
            return None, "Dropdown mata kuliah tidak ditemukan di form MIS."
        course_value = _pick_course_value(parsed_form.course_options, entry.course_keyword)
        if not course_value:
            return None, f"Mata kuliah dengan keyword '{entry.course_keyword}' tidak ditemukan."
        payload[parsed_form.course_field] = course_value

    if parsed_form.agree_field:
        if entry.agree:
            payload[parsed_form.agree_field] = parsed_form.agree_value or "on"
        else:
            payload.pop(parsed_form.agree_field, None)

    return payload, None


def _looks_like_cas_login_page(html: str, url: str) -> bool:
    combined = _normalize(f"{url} {html}")
    return "central authentication service" in combined and "name=\"lt\"" in combined


def _looks_like_mis_login_required(html: str) -> bool:
    return _contains_any(
        html,
        [
            "anda harus login terlebih",
            "anda harus login terlebih dauhulu",
            "<h2>anda harus login",
        ],
    )


def _extract_cas_error(html: str) -> Optional[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    status = soup.find(id="status")
    if isinstance(status, Tag):
        text = _clean_text(status.get_text(" ", strip=True))
        return text or None
    text = _clean_text(soup.get_text(" ", strip=True))
    if "cannot be determined to be authentic" in text.lower():
        return "Username/password CAS tidak valid."
    return None


def _contains_submit_error(html: str) -> Optional[str]:
    text = _normalize(BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True))
    error_signals = [
        "harus diisi",
        "wajib diisi",
        "format =",
        "maksimal",
        "error",
        "gagal",
    ]
    if any(signal in text for signal in error_signals):
        return "Form logbook ditolak server MIS. Cek isi jam/kegiatan lalu coba lagi."
    return None


def _page_diag(url: str, html: str) -> str:
    title = _page_title(html)
    text = _clean_text(BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True))
    short_text = text[:180] + ("..." if len(text) > 180 else "")
    return f"url={url or '-'} | title={title} | text={short_text}"


def _extract_ajax_logbook_params(html: str) -> Optional[Tuple[str, str, str]]:
    """Extract (valTahun, valSemester, valMinggu) from onload="showEntry_Logbook_KP1(Y, S, W)"."""
    m = re.search(r"showEntry_Logbook_KP1\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", html or "")
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


def _extract_frame_sources(html: str, page_url: str) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    sources: List[str] = []
    for tag in soup.find_all(["iframe", "frame"]):
        if not isinstance(tag, Tag):
            continue
        src = _clean_text(str(tag.get("src") or ""))
        if not src or src.startswith("#") or src.lower().startswith("javascript:"):
            continue
        sources.append(urljoin(page_url, src))
    unique: List[str] = []
    seen = set()
    for src in sources:
        if src not in seen:
            seen.add(src)
            unique.append(src)
    return unique


def _fetch_logbook_kp1_ajax_page(
    session: requests.Session,
    base_url: str,
    shell_html: str,
    timeout: Tuple[int, int],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Fetch entry_logbook_kp1.php and return a context dict with all metadata needed
    to submit logbook data via the simpan_data_logbook1 AJAX mechanism.

    Returns a dict with keys:
      val_tahun, val_semester, val_minggu  – from the shell page onload
      nrp_mahasiswa                        – student NRP (from Simpan button onclick)
      kp_daftar, mahasiswa                 – hidden fields from AJAX page
      tanggal                              – current date value (YYYY-MM-DD)
      course_options                        – list of (value, label) tuples from matakuliah select
      ajax_base_url                        – base URL for entry_logbook_kp1.php
    """
    ajax_params = _extract_ajax_logbook_params(shell_html)
    if not ajax_params:
        return None, "Tidak bisa ekstrak parameter AJAX logbook dari halaman shell MIS."

    val_tahun, val_semester, val_minggu = ajax_params
    ajax_base = urljoin(base_url, "entry_logbook_kp1.php")
    ajax_url = f"{ajax_base}?valTahun={val_tahun}&valSemester={val_semester}&valMinggu={val_minggu}"

    try:
        ajax_resp = session.get(ajax_url, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        return None, f"Gagal fetch halaman AJAX logbook MIS: {exc}"

    if ajax_resp.status_code >= 400:
        return None, f"Halaman AJAX logbook MIS error HTTP {ajax_resp.status_code}."
    if _looks_like_cas_login_page(ajax_resp.text, ajax_resp.url or ""):
        return None, "Session CAS berakhir saat fetch AJAX logbook."

    soup = BeautifulSoup(ajax_resp.text, "html.parser")

    def _field_value(name: str) -> str:
        tag = soup.find("input", {"name": name})
        return str(tag.get("value") or "") if isinstance(tag, Tag) else ""

    kp_daftar = _field_value("kp_daftar")
    mahasiswa = _field_value("mahasiswa")
    tanggal = _field_value("tanggal")  # e.g. "2026-03-03"

    # Extract valnrpMahasiswa from the Simpan button onclick:
    # onclick="simpan_data_logbook1(2423600049, 2025, 2, 9)"
    nrp_mahasiswa = ""
    m = re.search(
        r"simpan_data_logbook1\s*\(\s*(\d+)\s*,",
        ajax_resp.text or "",
    )
    if m:
        nrp_mahasiswa = m.group(1)

    # Course options from matakuliah dropdown
    course_options: List[Tuple[str, str]] = []
    matkul_sel = soup.find("select", {"name": "matakuliah"})
    if isinstance(matkul_sel, Tag):
        for opt in matkul_sel.find_all("option"):
            if isinstance(opt, Tag):
                val = str(opt.get("value") or "").strip()
                label = _clean_text(opt.get_text(" ", strip=True))
                course_options.append((val, label))

    if not kp_daftar and not mahasiswa and not nrp_mahasiswa:
        return None, "Tidak bisa membaca data mahasiswa dari halaman AJAX logbook MIS. Mungkin belum terdaftar KP."

    return {
        "val_tahun": val_tahun,
        "val_semester": val_semester,
        "val_minggu": val_minggu,
        "nrp_mahasiswa": nrp_mahasiswa,
        "kp_daftar": kp_daftar,
        "mahasiswa": mahasiswa,
        "tanggal": tanggal,
        "course_options": course_options,
        "ajax_base_url": ajax_base,
    }, None


def _submit_logbook_kp1_ajax(
    session: requests.Session,
    entry: "LogbookEntry",
    page_ctx: Dict[str, Any],
    timeout: Tuple[int, int],
) -> Tuple[bool, str]:
    """Submit logbook data by replicating the simpan_data_logbook1 JS function.

    The browser's onclick handler calls:
        simpan_data_logbook1(nrp, tahun, semester, minggu)
    which POSTs URL-encoded data to entry_logbook_kp1.php.
    We replicate that exact request.
    """
    from urllib.parse import urlencode

    course_value = _pick_course_value(page_ctx["course_options"], entry.course_keyword)
    if entry.related and not course_value:
        options_preview = ", ".join(t for _, t in page_ctx["course_options"][:8])
        return False, f"Mata kuliah dengan keyword '{entry.course_keyword}' tidak ditemukan. Pilihan: {options_preview}"

    # Replicate JavaScript: kegiatan.replace("&","dan")
    kegiatan = entry.activity.replace("&", "dan")

    sesuai_kuliah = "1" if entry.related else "2"  # radio value: Ya=1, Tidak=2

    params: Dict[str, str] = {
        "valnrpMahasiswa": page_ctx["nrp_mahasiswa"],
        "valTahun": page_ctx["val_tahun"],
        "valSemester": page_ctx["val_semester"],
        "Simpan": "1",
        "valMinggu": page_ctx["val_minggu"],
        "tanggal": page_ctx["tanggal"],  # use server-provided date (YYYY-MM-DD)
        "jam_mulai": entry.start_time,
        "jam_selesai": entry.end_time,
        "kegiatan": kegiatan,
        "sesuai_kuliah": sesuai_kuliah,
        "matakuliah": course_value or "",
        "kp_daftar": page_ctx["kp_daftar"],
        "mahasiswa": page_ctx["mahasiswa"],
        "Setuju": "1",
    }

    ajax_url = page_ctx["ajax_base_url"]
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        resp = session.post(ajax_url, data=params, headers=headers, timeout=timeout, allow_redirects=True)
    except requests.exceptions.RequestException as exc:
        return False, f"Gagal submit logbook MIS: {exc}"

    if resp.status_code >= 400:
        return False, f"Submit logbook gagal HTTP {resp.status_code}."
    if _looks_like_cas_login_page(resp.text, resp.url or ""):
        return False, "Submit gagal karena session CAS berakhir."
    if _looks_like_mis_login_required(resp.text):
        return False, "Submit gagal karena session MIS tidak aktif."

    text = _normalize(BeautifulSoup(resp.text or "", "html.parser").get_text(" ", strip=True))
    success_hints = ["berhasil", "tersimpan", "sukses", "disimpan"]
    if any(hint in text for hint in success_hints):
        return True, "Logbook berhasil disubmit."

    # If the response looks like the updated logbook table (contains the
    # activity text we just submitted), treat as success.
    if kegiatan[:30].lower() in text:
        return True, "Logbook berhasil disubmit."

    error_signals = ["harus diisi", "wajib diisi", "format =", "maksimal"]
    if any(sig in text for sig in error_signals):
        return False, "Form logbook ditolak server MIS. Cek isi jam/kegiatan."

    return True, "Submit logbook terkirim. Cek halaman MIS untuk verifikasi final."



def _resolve_logbook_form_with_frames(
    session: requests.Session,
    base_url: str,
    html: str,
    timeout: Tuple[int, int],
) -> Tuple[Optional[ParsedLogbookForm], Optional[str]]:
    """Try to parse the logbook form directly, then via iframes.
    
    Note: for MIS logbook KP1 pages that use JS/AJAX to render the form,
    use _fetch_logbook_kp1_ajax_page + _submit_logbook_kp1_ajax instead.
    """
    parsed_form, parse_error = parse_logbook_form(html, base_url)
    if parsed_form is not None:
        return parsed_form, None

    frame_sources = _extract_frame_sources(html, base_url)
    frame_diags: List[str] = []
    for frame_url in frame_sources[:8]:
        try:
            frame_resp = session.get(frame_url, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            frame_diags.append(f"{frame_url} -> {exc}")
            continue
        if frame_resp.status_code >= 400:
            frame_diags.append(f"{frame_url} -> HTTP {frame_resp.status_code}")
            continue
        if _looks_like_cas_login_page(frame_resp.text, frame_resp.url or ""):
            frame_diags.append(f"{frame_url} -> redirected CAS login")
            continue
        candidate, candidate_error = parse_logbook_form(frame_resp.text, frame_resp.url or frame_url)
        if candidate is not None:
            return candidate, None
        frame_diags.append(
            "{url} -> {error} | {diag}".format(
                url=frame_url,
                error=candidate_error or "parse gagal",
                diag=_page_diag(frame_resp.url or frame_url, frame_resp.text),
            )
        )

    details: List[str] = []
    if frame_sources:
        details.append("Frame checked: " + " || ".join(frame_diags[:3]))
    else:
        details.append("Tidak ada iframe/frame source terdeteksi.")

    message = parse_error or "Form logbook MIS tidak bisa diparsing."
    message += " " + " | ".join(details)
    return None, message


def submit_logbook_entry(entry: LogbookEntry, config: LogbookConfig) -> Tuple[bool, str]:
    if not config.cas_login_url.strip():
        return False, "LOGBOOK_CAS_LOGIN_URL belum di-set."
    if not config.form_url.strip():
        return False, "LOGBOOK_FORM_URL belum di-set."
    if not config.username.strip():
        return False, "LOGBOOK_CAS_USERNAME belum di-set."
    if not config.password.strip():
        return False, "LOGBOOK_CAS_PASSWORD belum di-set."

    timeout = (config.timeout_connect, config.timeout_read)
    session = requests.Session()

    try:
        cas_page = session.get(config.cas_login_url, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        return False, f"Gagal membuka halaman login CAS: {exc}"
    if cas_page.status_code >= 400:
        return False, f"Halaman login CAS error HTTP {cas_page.status_code}."

    cas_action_url, cas_payload, cas_error = parse_cas_login_form(cas_page.text, cas_page.url or config.cas_login_url)
    if cas_error or not cas_action_url or cas_payload is None:
        return False, cas_error or "Form login CAS tidak valid."

    cas_payload = dict(cas_payload)
    if "username" in cas_payload:
        cas_payload["username"] = config.username
    else:
        user_key = next((key for key in cas_payload if "user" in key.lower()), "username")
        cas_payload[user_key] = config.username

    if "password" in cas_payload:
        cas_payload["password"] = config.password
    else:
        pass_key = next((key for key in cas_payload if "pass" in key.lower()), "password")
        cas_payload[pass_key] = config.password

    try:
        login_resp = session.post(cas_action_url, data=cas_payload, timeout=timeout, allow_redirects=True)
    except requests.exceptions.RequestException as exc:
        return False, f"Gagal kirim login CAS: {exc}"

    if login_resp.status_code >= 400:
        return False, f"Login CAS gagal HTTP {login_resp.status_code}."
    if _looks_like_cas_login_page(login_resp.text, login_resp.url or ""):
        detail = _extract_cas_error(login_resp.text) or "Autentikasi CAS gagal."
        return False, detail

    try:
        form_page = session.get(config.form_url, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        return False, f"Gagal membuka halaman logbook MIS: {exc}"
    if form_page.status_code >= 400:
        return False, f"Halaman logbook MIS error HTTP {form_page.status_code}."
    if _looks_like_cas_login_page(form_page.text, form_page.url or ""):
        return False, "Session CAS tidak valid saat membuka logbook MIS."
    if _looks_like_mis_login_required(form_page.text):
        return False, "Session login belum masuk ke MIS. Cek URL service CAS."

    form_base_url = form_page.url or config.form_url

    # --- Primary path: AJAX-based submission (used by mEntry_Logbook_KP1.php) ---
    # The MIS logbook KP1 page uses JavaScript to load the form via AJAX from
    # entry_logbook_kp1.php and submit it with the simpan_data_logbook1 function.
    # We replicate this mechanism directly instead of parsing the HTML form.
    ajax_ctx, ajax_ctx_error = _fetch_logbook_kp1_ajax_page(
        session=session,
        base_url=form_base_url,
        shell_html=form_page.text,
        timeout=timeout,
    )
    if ajax_ctx is not None:
        return _submit_logbook_kp1_ajax(
            session=session,
            entry=entry,
            page_ctx=ajax_ctx,
            timeout=timeout,
        )

    # --- Fallback: traditional HTML form parse + POST ---
    parsed_form, parse_error = _resolve_logbook_form_with_frames(
        session=session,
        base_url=form_base_url,
        html=form_page.text,
        timeout=timeout,
    )
    if parse_error or parsed_form is None:
        diag = _page_diag(form_page.url or config.form_url, form_page.text)
        ctx_msg = f" (AJAX ctx error: {ajax_ctx_error})" if ajax_ctx_error else ""
        return False, (parse_error or "Form logbook MIS tidak bisa diparsing.") + f"{ctx_msg} [{diag}]"

    payload, payload_error = build_logbook_payload(parsed_form, entry)
    if payload_error or payload is None:
        return False, payload_error or "Payload logbook tidak valid."

    try:
        submit_resp = session.post(parsed_form.action_url, data=payload, timeout=timeout, allow_redirects=True)
    except requests.exceptions.RequestException as exc:
        return False, f"Gagal submit logbook MIS: {exc}"

    if submit_resp.status_code >= 400:
        return False, f"Submit logbook gagal HTTP {submit_resp.status_code}."
    if _looks_like_cas_login_page(submit_resp.text, submit_resp.url or ""):
        return False, "Submit gagal karena session CAS berakhir."
    if _looks_like_mis_login_required(submit_resp.text):
        return False, "Submit gagal karena session MIS tidak aktif."

    submit_error = _contains_submit_error(submit_resp.text)
    if submit_error:
        return False, submit_error

    success_hints = ["berhasil", "tersimpan", "sukses", "disimpan"]
    text = _normalize(BeautifulSoup(submit_resp.text or "", "html.parser").get_text(" ", strip=True))
    if any(hint in text for hint in success_hints):
        return True, "Logbook berhasil disubmit."

    return True, "Submit logbook terkirim. Cek halaman MIS untuk verifikasi final."


# ---------------------------------------------------------------------------
# File upload
# ---------------------------------------------------------------------------

class LogbookFileType:
    PDF = "pdf"    # Progres laporan KP (PDF, max 1 MB) → field fileupload / kirimfile1
    PHOTO = "photo"  # Foto kegiatan KP (JPG/JPEG, max 500 KB) → field fileupload2 / kirimfile2


def upload_logbook_file(
    file_bytes: bytes,
    filename: str,
    file_type: str,
    config: LogbookConfig,
) -> Tuple[bool, str]:
    """Upload logbook file (PDF laporan or JPG foto) to MIS.

    Replicates browser kirim_file1() / kirim_file2() which set kirimfile1/kirimfile2=1
    and submit the parent <form id="kirim"> as multipart/form-data to mEntry_Logbook_KP1.php.

    Args:
        file_bytes: Raw bytes of the file to upload.
        filename: Original filename including extension.
        file_type: LogbookFileType.PDF or LogbookFileType.PHOTO.
        config: LogbookConfig with CAS credentials.

    Returns:
        (success, message)
    """
    if not file_bytes:
        return False, "File kosong, tidak ada yang diunggah."

    # Validate extension / mimetype
    fn_lower = filename.lower() if filename else ""
    if file_type == LogbookFileType.PDF:
        if not fn_lower.endswith(".pdf"):
            return False, "File progres laporan harus berekstensi PDF."
        file_field = "fileupload"
        trigger_field = "kirimfile1"
        mimetype = "application/pdf"
        max_bytes = 1 * 1024 * 1024  # 1 MB
        label = "laporan PDF"
    elif file_type == LogbookFileType.PHOTO:
        if not any(fn_lower.endswith(ext) for ext in (".jpg", ".jpeg")):
            return False, "Foto kegiatan harus berekstensi JPG/JPEG."
        file_field = "fileupload2"
        trigger_field = "kirimfile2"
        mimetype = "image/jpeg"
        max_bytes = 500 * 1024  # 500 KB
        label = "foto JPG"
    else:
        return False, f"Tipe file tidak dikenal: {file_type!r}."

    if len(file_bytes) > max_bytes:
        size_kb = len(file_bytes) // 1024
        max_kb = max_bytes // 1024
        return False, f"Ukuran file {size_kb} KB melebihi batas {max_kb} KB untuk {label}."

    timeout = (config.timeout_connect, config.timeout_read)
    session = requests.Session()

    # CAS login
    try:
        cas_page = session.get(config.cas_login_url, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        return False, f"Gagal membuka halaman login CAS: {exc}"
    if cas_page.status_code >= 400:
        return False, f"Halaman login CAS error HTTP {cas_page.status_code}."

    cas_action_url, cas_payload, cas_error = parse_cas_login_form(
        cas_page.text, cas_page.url or config.cas_login_url
    )
    if cas_error or not cas_action_url or cas_payload is None:
        return False, cas_error or "Form login CAS tidak valid."

    cas_payload = dict(cas_payload)
    user_key = "username" if "username" in cas_payload else next(
        (k for k in cas_payload if "user" in k.lower()), "username"
    )
    pass_key = "password" if "password" in cas_payload else next(
        (k for k in cas_payload if "pass" in k.lower()), "password"
    )
    cas_payload[user_key] = config.username
    cas_payload[pass_key] = config.password

    try:
        login_resp = session.post(cas_action_url, data=cas_payload, timeout=timeout, allow_redirects=True)
    except requests.exceptions.RequestException as exc:
        return False, f"Gagal login CAS: {exc}"
    if login_resp.status_code >= 400:
        return False, f"Login CAS gagal HTTP {login_resp.status_code}."
    if _looks_like_cas_login_page(login_resp.text, login_resp.url or ""):
        return False, _extract_cas_error(login_resp.text) or "Autentikasi CAS gagal."

    # Fetch shell page + AJAX page to get hidden field values
    try:
        shell = session.get(config.form_url, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        return False, f"Gagal membuka halaman logbook MIS: {exc}"
    if shell.status_code >= 400:
        return False, f"Halaman logbook MIS error HTTP {shell.status_code}."
    if _looks_like_mis_login_required(shell.text):
        return False, "Session MIS tidak aktif saat upload file."

    ajax_ctx, ctx_error = _fetch_logbook_kp1_ajax_page(
        session=session,
        base_url=shell.url or config.form_url,
        shell_html=shell.text,
        timeout=timeout,
    )
    if ajax_ctx is None:
        return False, ctx_error or "Gagal fetch data dari halaman AJAX logbook."

    # POST multipart to mEntry_Logbook_KP1.php (the parent shell form)
    # replicating: document.getElementById('kirimfileX').value=1; document.kirim.submit();
    shell_url = shell.url or config.form_url
    data_fields = {
        trigger_field: "1",
        "kp_daftar": ajax_ctx["kp_daftar"],
        "mahasiswa": ajax_ctx["mahasiswa"],
        "tanggal": ajax_ctx["tanggal"],
        "minggu": ajax_ctx["val_minggu"],
        "tahun": ajax_ctx["val_tahun"],
        "cbSemester": ajax_ctx["val_semester"],
    }
    files_mp = {
        file_field: (filename, file_bytes, mimetype),
    }
    # Add text fields as (None, value) tuples alongside the actual file tuple
    for key, val in data_fields.items():
        files_mp[key] = (None, val)  # type: ignore[assignment]

    try:
        up_resp = session.post(shell_url, files=files_mp, timeout=timeout, allow_redirects=True)
    except requests.exceptions.RequestException as exc:
        return False, f"Gagal upload file ke MIS: {exc}"

    if up_resp.status_code >= 400:
        return False, f"Upload file gagal HTTP {up_resp.status_code}."
    if _looks_like_cas_login_page(up_resp.text, up_resp.url or ""):
        return False, "Session CAS berakhir saat upload file."

    resp_text = _normalize(BeautifulSoup(up_resp.text or "", "html.parser").get_text(" ", strip=True))
    success_hints = ["berhasil", "tersimpan", "sukses", "disimpan", "terunggah", "uploaded"]
    if any(h in resp_text for h in success_hints):
        return True, f"File {label} berhasil diunggah."

    error_hints = ["gagal", "error", "tidak valid", "tidak diizinkan", "melebihi"]
    for hint in error_hints:
        if hint in resp_text:
            return False, f"Upload {label} ditolak server MIS. Pastikan format dan ukuran file sesuai."

    # Treat as success if page reloaded correctly (no error, not login page)
    if not _looks_like_mis_login_required(up_resp.text):
        return True, f"File {label} terkirim ke MIS. Cek halaman untuk verifikasi."

    return False, f"Upload {label} kemungkinan gagal. Cek halaman MIS secara manual."

