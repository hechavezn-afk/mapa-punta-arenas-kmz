"""Text extraction helpers for customer service notice PDFs."""
from __future__ import annotations

from datetime import datetime
from io import BytesIO
import re
import unicodedata

from pypdf import PdfReader


class NoticePdfError(ValueError):
    pass


def _clean(value: str | None) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip(" :-\n\t")
    return "" if value.lower() in {"n/a", "na", "-", "s/i"} else value


def _capture(text: str, pattern: str) -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    return _clean(match.group(1)) if match else ""


def _date(raw: str) -> str | None:
    raw = _clean(raw)
    for pattern in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, pattern).date().isoformat()
        except ValueError:
            continue
    return raw or None


def _priority(raw: str) -> str | None:
    normalized = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode().upper()
    normalized = re.sub(r"[^A-Z]+", " ", normalized).strip()
    if not normalized:
        return None
    if "URGENTE" in normalized or "CRITICA" in normalized or "CRITICO" in normalized:
        return "Urgente"
    if "MEDIA BAJA" in normalized or "MEDIO BAJO" in normalized:
        return "Media baja"
    if "MEDIA ALTA" in normalized or "MEDIO ALTO" in normalized:
        return "Media alta"
    if "ALTA" in normalized or "ALTO" in normalized:
        return "Alta"
    if "BAJA" in normalized or "BAJO" in normalized:
        return "Baja"
    if "MEDIA" in normalized or "MEDIO" in normalized:
        return "Media"
    return None


def extract_notice_pdf(file_bytes: bytes) -> dict:
    """Extract likely header fields and narrative, leaving uncertain fields blank."""
    if not file_bytes:
        raise NoticePdfError("El archivo PDF está vacío.")
    try:
        reader = PdfReader(BytesIO(file_bytes), strict=False)
    except Exception as exc:
        raise NoticePdfError("No pude abrir este PDF. Verifica que el archivo no esté dañado.") from exc
    if len(reader.pages) > 40:
        raise NoticePdfError("El PDF supera el máximo de 40 páginas para importar un aviso.")
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if len(re.sub(r"\s", "", text)) < 40:
        raise NoticePdfError("El PDF no contiene texto seleccionable. Este tipo de documento necesita OCR antes de importarlo.")

    notice = _capture(text, r"N.{0,5}?de aviso\s*:\s*([\d-]+)")
    notice_header = re.search(
        r"N.{0,5}?de aviso\s*:\s*[\d-]+\s+Fecha\s*:\s*([\d./-]+).*?Prioridad\s*:\s*([^\n]+?)\s+Fin deseado aviso\s*:\s*([\d./-]+)",
        text, flags=re.IGNORECASE | re.DOTALL)
    notice_date = _date(notice_header.group(1)) if notice_header else None
    raw_priority = _clean(notice_header.group(2)) if notice_header else ""
    desired_end = _date(notice_header.group(3)) if notice_header else None

    locality_raw = _capture(text, r"Comuna\s*:\s*(.+?)\s+(?:Subestaci[oó�]n|$)")
    locality_normalized = unicodedata.normalize("NFKD", locality_raw).encode("ascii", "ignore").decode().upper()
    locality = "Puerto Natales" if "NATALES" in locality_normalized else None

    narrative = ""
    # These forms visually letter-space the section title in their text layer.
    headings = list(re.finditer(r"T\s*E\s*X\s*T\s*O\s*A\s*M\s*P\s*L\s*I\s*A\s*D\s*O", text, flags=re.IGNORECASE))
    if headings:
        start = headings[-1].end()
        tail = text[start:]
        end = re.search(r"M\s*E\s*D\s*I\s*D\s*A\s*S", tail, flags=re.IGNORECASE)
        narrative = _clean(tail[:end.start()] if end else tail)
        narrative = re.sub(r"^\([^)]*\)\s*", "", narrative)
        narrative = re.sub(r"\s+", " ", narrative)
        narrative = re.sub(r"\bmese\s+s\b", "meses", narrative, flags=re.IGNORECASE)
        narrative = re.sub(r"\btiem\s+po\b", "tiempo", narrative, flags=re.IGNORECASE)
        narrative = re.sub(r"\bv\s+enir\b", "venir", narrative, flags=re.IGNORECASE)
        narrative = re.sub(r"D\s*I?RECCI[ÓO�]N", "DIRECCIÓN", narrative, flags=re.IGNORECASE)

    measures_text = ""
    measures_match = re.search(r"M\s*E\s*D\s*I\s*D\s*A\s*S\s*(.*?)(?:NOMBRE\s*:|FIRMA DEL CLIENTE|$)", text,
                               flags=re.IGNORECASE | re.DOTALL)
    if measures_match:
        measures_text = _clean(measures_match.group(1))
    measure_options = [
        "Inspección en terreno", "Medición termográfica", "Confirmar factibilidad apoyo",
        "Efectuar presupuesto", "Emitir factura", "Reparación/Mantenimiento",
    ]
    comparable_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    printed_measure_options = [option for option in measure_options
                               if unicodedata.normalize("NFKD", option).encode("ascii", "ignore").decode().casefold()
                               in comparable_text]
    # The standard notice form prints these options without checkmarks. Do not
    # claim a measure was selected unless the PDF has an explicit mark.
    measure_choices = []

    reported_asset = _capture(narrative, r"N[°º�]\s*POSTE\s*N?[°º�]?\s*[:#]?\s*([A-Z0-9][\w.-]*)")
    if not reported_asset:
        reported_asset = _capture(text, r"N[°º�]\s*(?:DE\s+)?POSTE\s*N?[°º�]?\s*[:#]?\s*([A-Z0-9][\w.-]*)")
    general_asset = _capture(text, r"N.{0,5}?Poste\s*:\s*([\d.-]+)")
    contact_name = _capture(text, r"Pers\.\s*Contacto\s*:\s*([^\n]+?)\s+(?:Consecutivo|Direcci[oó�]n Servicio)")
    if not contact_name:
        contact_name = _capture(text, r"SOLICITANTE\s*:\s*([^\n]+?)\s+(?:MEDIDOR|N.{0,5}?POSTE)")
    contact_phone_value = _capture(text, r"Tel[eé�]fono contacto\s*:\s*([^:\n]*?)\s*(?:Comuna\s*:|Subestaci[oó�]n)")
    contact_phone = contact_phone_value if re.search(r"\d", contact_phone_value) else ""
    customer_phone = _capture(text, r"Tel[eé�]fono cliente\s*:\s*([^\n]+?)\s+(?:D A T O S|N.{0,5}?Instalaci[oó�]n)")
    installation = _capture(text, r"N.{0,5}?Instalaci[oó�]n\s*:\s*([A-Z0-9-]+)")
    service_address = _capture(text, r"Direcci[oó�]n Servicio\s*:\s*([^\n]+?)\s+(?:Sigla Edif\.|Tel[eé�]fono contacto)")
    narrative_address = _capture(narrative, r"D\s*I?RECCI[ÓO�]N\s*:\s*(.+?)(?:\s+SOLICITANTE\s*:|$)")
    responsible = _capture(text, r"Respons\. aviso\s*:\s*([^\n]+?)\s+(?:Clase Pto\.Sum\.|N.{0,5}?S\.E\.C)")
    meter_number = _capture(text, r"N.{0,5}?Medidor 1\s*:\s*([\w-]+)")
    meter_brand = _capture(text, r"N.{0,5}?Medidor 1\s*:\s*[\w-]+\s+Marca\s*:\s*([^\n]+?)\s+Modelo")
    meter_model = _capture(text, r"Modelo\s*:\s*([^\n]+?)\s+Propiedad med\.")
    connected_power = _capture(text, r"Pot\.Conectada\s*:\s*([\d.,]+)")
    service_class = _capture(text, r"Clase Pto\.Sum\.\s*:\s*([^\s]+)")
    tariff = _capture(text, r"Tarifa\s*:\s*(.+?)\s+Porci[oó�]n")
    customer_ids = _capture(text, r"Cliente\s*:\s*(.+?)\s+Tel[eé�]fono cliente\s*:")
    notice_type = _capture(text, r"AVISO DE\s*:\s*([^\n]+)")

    return {
        "notice_number": notice,
        "notice_date": notice_date,
        "desired_end_date": desired_end,
        "priority_raw": raw_priority or None,
        "priority": _priority(raw_priority),
        "notice_type": notice_type or None,
        "locality": locality,
        "commune_raw": locality_raw or None,
        "contact_name": contact_name or None,
        "contact_phone": contact_phone or None,
        "customer_phone": customer_phone or None,
        "customer_ids_raw": customer_ids or None,
        "responsible": responsible or None,
        "service_address": service_address or None,
        "narrative_address": narrative_address or None,
        "installation_number": installation or None,
        "reported_asset_code": reported_asset or None,
        "general_asset_code": general_asset or None,
        "meter_number": meter_number or None,
        "meter_brand": meter_brand or None,
        "meter_model": meter_model or None,
        "connected_power": connected_power or None,
        "service_class": service_class or None,
        "tariff": tariff or None,
        "narrative": narrative or None,
        "measures_selected": measure_choices,
        "measure_options_printed": printed_measure_options,
        "measures_text_raw": measures_text or None,
        "page_count": len(reader.pages),
    }
