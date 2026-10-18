"""Nettoyage de texte (BeautifulSoup), pré-filtrage par mots-clés, outils d'adresses."""
import html as htmllib
import re
import unicodedata
from email.utils import getaddresses
from urllib.parse import urlparse

from bs4 import BeautifulSoup

GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "outlook.fr", "hotmail.com", "hotmail.fr",
    "live.com", "live.fr", "msn.com", "yahoo.com", "yahoo.fr", "icloud.com", "me.com",
    "proton.me", "protonmail.com", "orange.fr", "wanadoo.fr", "free.fr", "sfr.fr",
    "laposte.net", "gmx.com", "gmx.fr", "aol.com",
}


# --------------------------------------------------------------------------- mots-clés
def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def normalize(text: str) -> str:
    return strip_accents(text or "").lower()


def contains_keyword(text: str, keywords: list[str]) -> bool:
    """Pré-filtrage local (sans accent, sans casse) avant tout appel à l'IA."""
    haystack = normalize(text)
    return any(normalize(k) in haystack for k in keywords if k)


# --------------------------------------------------------------------------- secteur d'activité
# Heuristique de repli (sans IA) : mots-clés -> secteur. L'IA locale (local_ai) fait mieux.
# Ordre du plus spécifique au plus générique : le premier secteur qui matche est retenu.
_SECTOR_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Robotique", ("robot", "cobot", "robotis", "prehenseur", "manipulateur", "actionneur")),
    ("Vision / Perception", ("vision", "perception", "lidar", "camera", "imagerie", "traitement d'image",
                             "point cloud", "nuage de points")),
    ("IA / Data", ("intelligence artificielle", "machine learning", "deep learning", "data science",
                   "donnees", "apprentissage", "modele predictif")),
    ("Systèmes embarqués / IoT", ("embarque", "esp32", "arduino", "iot", "microcontroleur", "firmware",
                                  "capteur", "electronique", "temps reel")),
    ("Cybersécurité", ("cybersecurite", "securite informatique", "pentest", "vulnerabilite", "malware",
                       "pare-feu", "soc ")),
    ("Cloud / DevOps", ("devops", "kubernetes", "docker", "ci/cd", "cloud", "terraform", "ansible", "aws", "azure")),
    ("Web / Full-stack", ("site web", "frontend", "front-end", "back-end", "backend", "react", "angular",
                          "javascript", "saas", "application web", "html")),
    ("Mobile", ("mobile", "android", "swift", "kotlin", "flutter")),
    ("Jeux vidéo", ("jeu video", "jeux video", "unity", "unreal", "gaming", "game")),
    ("Réseau / Infrastructure", ("reseau", "infrastructure", "serveur", "sysadmin", "proxmox",
                                 "virtualisation", "administration systeme")),
    ("Recherche / Labo", ("laboratoire", "labo", "inria", "cnrs", "these", "publication", "universite",
                          "academique", "doctorat", "postdoc")),
    ("Développement logiciel", ("developpement", "logiciel", "application", "python", "java", "c++")),
)


def guess_sector(text: str) -> str | None:
    """Devine le secteur d'activité par mots-clés (repli quand l'IA locale est indisponible)."""
    haystack = normalize(text)
    for sector, words in _SECTOR_KEYWORDS:
        if any(word in haystack for word in words):
            return sector
    return None



# --------------------------------------------------------------------------- HTML -> texte
def html_to_text(html: str, drop_quotes: bool = True) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "head", "title"]):
        tag.decompose()
    if drop_quotes:
        for tag in soup.select("div.gmail_quote, blockquote"):
            tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(["p", "div", "li", "tr", "h1", "h2", "h3", "h4"]):
        tag.append("\n")
    text = soup.get_text("")
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_QUOTE_FR = re.compile(r"^\s*Le\s.{4,200}?a\s+[ée]crit\s*:?\s*$", re.I)
_QUOTE_EN = re.compile(r"^\s*On\s.{4,200}?wrote:?\s*$", re.I)
_QUOTE_SEP = re.compile(
    r"^\s*-{2,}\s*(Message d.origine|Original Message|Message transf[ée]r[ée]|Forwarded message)", re.I
)
_OUTLOOK_FROM = re.compile(r"^\s*(De|From)\s*:\s*\S+", re.I)
_OUTLOOK_NEXT = re.compile(r"^\s*(Envoy[ée]|Sent|Date|[ÀA]|To|Objet|Subject)\s*:", re.I)


def strip_quoted_text(text: str) -> str:
    """Retire la partie citée (« Le … a écrit : », « > … », en-têtes Outlook)."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    kept: list[str] = []
    for i, line in enumerate(lines):
        if line.lstrip().startswith(">"):
            break
        two_lines = (line.strip() + " " + (lines[i + 1].strip() if i + 1 < len(lines) else "")).strip()
        if (
            _QUOTE_SEP.match(line)
            or _QUOTE_FR.match(line) or _QUOTE_FR.match(two_lines)
            or _QUOTE_EN.match(line) or _QUOTE_EN.match(two_lines)
        ):
            break
        if _OUTLOOK_FROM.match(line) and any(_OUTLOOK_NEXT.match(prev) for prev in lines[i + 1:i + 5]):
            break
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    return cleaned or (text or "").strip()[:2000]


def truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# --------------------------------------------------------------------------- HTML sortant
_ALLOWED_TAGS = {"p", "br", "b", "strong", "i", "em", "u", "ul", "ol", "li", "a", "div", "span", "blockquote", "h3"}


def sanitize_html(html: str) -> str:
    """Ne garde que des balises de mise en forme basiques (ce que l'éditeur peut produire)."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "iframe", "object", "embed", "form", "input", "button", "svg"]):
        tag.decompose()
    for tag in soup.find_all(True):
        if tag.name not in _ALLOWED_TAGS:
            tag.unwrap()
            continue
        href = tag.get("href") if tag.name == "a" else None
        tag.attrs = {}
        if href and re.match(r"^(https?://|mailto:)", href.strip(), re.I):
            tag.attrs["href"] = href.strip()
    return str(soup)


def build_email_html(editor_html: str) -> str:
    """HTML final envoyé par Gmail : balises nettoyées + mise en forme basique encapsulée."""
    soup = BeautifulSoup(sanitize_html(editor_html), "html.parser")
    for p in soup.find_all("p"):
        p["style"] = "margin:0 0 12px 0"
    for ul in soup.find_all(["ul", "ol"]):
        ul["style"] = "margin:0 0 12px 20px;padding:0"
    inner = str(soup)
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#222">'
        f"{inner}</div>"
    )


def text_to_html(text: str) -> str:
    """Texte brut (paragraphes séparés par une ligne vide) -> HTML pour l'éditeur."""
    paragraphs = [p for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]
    return "".join(
        "<p>" + "<br>".join(htmllib.escape(line.rstrip()) for line in p.strip().split("\n")) + "</p>"
        for p in paragraphs
    )


# --------------------------------------------------------------------------- adresses & domaines
def parse_addresses(*headers: str | None) -> list[tuple[str, str]]:
    """[(nom, adresse_en_minuscules)] pour un ou plusieurs en-têtes From/To/Cc."""
    pairs = getaddresses([h for h in headers if h])
    return [(name.strip(), addr.strip().lower()) for name, addr in pairs if "@" in addr]


def domain_of_email(address: str | None) -> str | None:
    if not address or "@" not in address:
        return None
    return address.rsplit("@", 1)[1].strip().lower() or None


def domain_of_url(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else (host or None)


def is_generic_domain(domain: str | None) -> bool:
    return not domain or domain in GENERIC_DOMAINS


def name_from_domain(domain: str | None) -> str:
    if not domain:
        return "Entreprise inconnue"
    parts = domain.split(".")
    core = parts[-2] if len(parts) >= 2 else parts[0]
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "gov", "ac"}:
        core = parts[-3]
    return core.replace("-", " ").replace("_", " ").title()
