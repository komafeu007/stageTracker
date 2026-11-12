"""Lettre de motivation : mise en page PDF (coordonnées en haut, corps rédigé par Gemini).

Le texte est rédigé par `gemini_client.draft_cover_letter`, relu/modifié par l'utilisateur,
puis transformé ici en PDF joint au mail. Rien n'est envoyé automatiquement.
"""
import re
import unicodedata
from datetime import date

from fpdf import FPDF

from .config import settings
from .db import kv_get

_MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
           "septembre", "octobre", "novembre", "décembre")

# Polices de base du PDF : encodage Windows-1252 (accents français, « », œ, €).
_ENCODING = "windows-1252"
_REPLACEMENTS = {" ": " ", " ": " ", "‑": "-", "‐": "-", "−": "-"}


def _pdf_text(text: str) -> str:
    """Rend le texte compatible avec les polices de base (caractère inconnu → '?')."""
    for src, dst in _REPLACEMENTS.items():
        text = text.replace(src, dst)
    return text.encode(_ENCODING, errors="replace").decode(_ENCODING)


def french_date(day: date | None = None) -> str:
    day = day or date.today()
    return f"{'1er' if day.day == 1 else day.day} {_MONTHS[day.month - 1]} {day.year}"


def sender_lines() -> list[str]:
    """Coordonnées de l'étudiant pour l'en-tête : nom, adresse, téléphone, e-mail, liens."""
    email = settings.user_email or kv_get("my_email", "") or ""
    lines = [settings.user_name, settings.user_address, settings.user_phone, email, *settings.user_links]
    return [line.strip() for line in lines if line and line.strip()]


def filename(company_name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", company_name or "").encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9-]+", "_", ascii_name).strip("_")
    return f"Lettre_de_motivation_{slug or 'entreprise'}.pdf"


class _LetterPDF(FPDF):
    def __init__(self) -> None:
        super().__init__(format="A4")
        self.core_fonts_encoding = _ENCODING
        self.set_margins(22, 20, 22)
        self.set_auto_page_break(True, margin=20)


def build_pdf(company: dict, subject: str, body: str, day: date | None = None) -> bytes:
    """Construit la lettre : expéditeur en haut à gauche, destinataire à droite, date, objet, corps."""
    pdf = _LetterPDF()
    pdf.set_title(_pdf_text(f"Lettre de motivation - {company.get('name') or ''}"))
    pdf.set_author(_pdf_text(settings.user_name))
    pdf.add_page()
    width = pdf.epw

    # Expéditeur (coordonnées)
    sender = sender_lines()
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(width, 6, _pdf_text(sender[0] if sender else ""), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    for line in sender[1:]:
        pdf.cell(width, 5, _pdf_text(line), new_x="LMARGIN", new_y="NEXT")

    # Destinataire, aligné à droite
    pdf.ln(8)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(width, 6, _pdf_text(company.get("name") or ""), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    if company.get("email"):
        pdf.cell(width, 5, _pdf_text(company["email"]), align="R", new_x="LMARGIN", new_y="NEXT")

    # Date et objet
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(width, 6, _pdf_text(f"Le {french_date(day)}"), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    pdf.set_font("Helvetica", "B", 11)
    pdf.multi_cell(width, 6, _pdf_text(f"Objet : {subject.strip()}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    # Corps
    pdf.set_font("Helvetica", "", 11)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body.replace("\r", "")) if p.strip()]
    for para in paragraphs:
        pdf.multi_cell(width, 5.6, _pdf_text(para), align="J", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3.5)

    # Signature
    pdf.ln(4)
    pdf.cell(width, 6, _pdf_text(settings.user_name), align="R", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
