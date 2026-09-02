#!/usr/bin/env python3
"""Pobiera dane o wydarzeniach z Indico (akademia.iitis.pl) i buduje z nich
lokalną kopię na stronie projektu.

Skrypt:
  * czyta publiczne API eksportu Indico (/export/event/<id>.json),
  * przelicza czasy z UTC na Europe/Warsaw (tak jak wyświetla je Indico),
  * pobiera lokalnie materiały inne niż wideo (z deduplikacją po sumie
    kontrolnej) do assets/materials/,
  * zapisuje _data/archiwum.json oraz strony-zaślepki w archiwum/.

Pliki wideo (ok. 37 GB) pozostają linkami do Indico - nie mieszczą się
w limitach GitHub Pages (100 MB/plik).

Wymagania: requests, markdown (opcjonalnie bleach).

Użycie:
    python3 tools/fetch_indico.py                 # pełne odświeżenie
    python3 tools/fetch_indico.py --no-download   # tylko metadane
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

try:  # opcjonalnie: sanityzacja HTML z opisów
    import bleach
except ImportError:  # pragma: no cover
    bleach = None

import markdown

BASE = "https://akademia.iitis.pl"
TZ = ZoneInfo("Europe/Warsaw")
ROOT = Path(__file__).resolve().parent.parent

# Wydarzenia projektu "Akademia Sztuki Kwantowej".
# Pomijamy konsultacje (event 25) - to inny projekt.
ONLINE_IDS = [7, 8, 12, 27]
WORKSHOP_IDS = [13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]

SLUGS = {
    7: "wyklady-online-termin-1",
    8: "wyklady-online-termin-2",
    12: "wyklady-online-termin-3",
    27: "wyklad-podsumowujacy",
    13: "perceptron-torun",
    14: "perceptron-chorzow",
    15: "perceptron-poznan",
    16: "uczenie-maszynowe-torun",
    17: "uczenie-maszynowe-krakow",
    18: "uczenie-maszynowe-warszawa",
    19: "wyzarzanie-chorzow",
    20: "wyzarzanie-poznan",
    21: "wyzarzanie-gdansk",
    22: "metody-jadrowe-krakow",
    23: "metody-jadrowe-warszawa",
    24: "metody-jadrowe-gdansk",
}

# Czego nie kopiujemy lokalnie (zostaje link do Indico).
NO_MIRROR_PREFIXES = ("video/", "audio/")
MIRROR_MAX_BYTES = 60 * 1024 * 1024

MATERIALS_DIR = ROOT / "assets" / "materials"
DATA_FILE = ROOT / "_data" / "archiwum.json"
# Materiały dokładane z repozytorium projektu do wydarzeń, które nie mają
# załączników w Indico (patrz klasa Uzupelnienia).
SUPPLEMENT_FILE = ROOT / "tools" / "materialy_lokalne.json"
SKIP_FILE = ROOT / "tools" / "pomijane_zalaczniki.json"
# Mapowanie MD5 -> URL dla nagrań przeniesionych poza Indico (patrz zenodo.py).
VIDEO_LINKS_FILE = ROOT / "_data" / "video_urls.json"
PAGES_DIR = ROOT / "archiwum"

MONTHS = {
    1: "stycznia", 2: "lutego", 3: "marca", 4: "kwietnia", 5: "maja",
    6: "czerwca", 7: "lipca", 8: "sierpnia", 9: "września", 10: "października",
    11: "listopada", 12: "grudnia",
}
MONTHS_NOM = {
    1: "styczeń", 2: "luty", 3: "marzec", 4: "kwiecień", 5: "maj",
    6: "czerwiec", 7: "lipiec", 8: "sierpień", 9: "wrzesień",
    10: "październik", 11: "listopad", 12: "grudzień",
}

TRANS = str.maketrans({
    "ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n", "ó": "o",
    "ś": "s", "ź": "z", "ż": "z",
    "Ą": "A", "Ć": "C", "Ę": "E", "Ł": "L", "Ń": "N", "Ó": "O",
    "Ś": "S", "Ź": "Z", "Ż": "Z",
})

session = requests.Session()


def _relax_tls_if_needed() -> None:
    """Serwer Indico bywa skonfigurowany bez pełnego łańcucha certyfikatów.
    Najpierw próbujemy normalnej weryfikacji, a dopiero w razie jej awarii
    wyłączamy ją - pobierane dane są publiczne i weryfikowane sumami MD5."""
    try:
        session.get(f"{BASE}/", timeout=30).raise_for_status()
        return
    except requests.exceptions.SSLError:
        pass
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session.verify = False
    print(f"Uwaga: {BASE} ma niepełny łańcuch certyfikatów - weryfikacja TLS wyłączona.")


def load_video_links() -> dict:
    """Nagrania nie mieszczą się w GitHub Pages, więc leżą w zewnętrznym
    repozytorium. Jeśli mapowanie istnieje, linkujemy tam zamiast do Indico."""
    if not VIDEO_LINKS_FILE.exists():
        return {"files": {}, "source_label": "Indico", "record_url": "", "doi": None}
    data = json.loads(VIDEO_LINKS_FILE.read_text(encoding="utf-8"))
    data.setdefault("files", {})
    data.setdefault("source_label", "Zenodo")
    return data


VIDEO_LINKS = load_video_links()


def ascii_fold(text: str) -> str:
    text = text.translate(TRANS)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def slugify(text: str, maxlen: int = 80) -> str:
    s = ascii_fold(text).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:maxlen].strip("-")


def safe_filename(name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = ascii_fold(stem)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or "plik"
    ext = re.sub(r"[^A-Za-z0-9]+", "", ascii_fold(ext)).lower()
    return f"{stem[:100]}.{ext}" if ext else stem[:100]


def to_local(node: dict | None) -> dict | None:
    """Indico zwraca czasy w UTC; przeliczamy na strefę wydarzenia."""
    if not node or not node.get("date"):
        return None
    time_part = (node.get("time") or "00:00:00").split(".")[0]
    dt = datetime.strptime(f"{node['date']} {time_part}", "%Y-%m-%d %H:%M:%S")
    dt = dt.replace(tzinfo=timezone.utc).astimezone(TZ)
    return {
        "date": dt.strftime("%Y-%m-%d"),
        "time": dt.strftime("%H:%M"),
        "iso": dt.isoformat(),
        "day_label": f"{dt.day} {MONTHS[dt.month]} {dt.year}",
    }


def date_range_label(start: dict, end: dict) -> str:
    s = datetime.fromisoformat(start["iso"])
    e = datetime.fromisoformat(end["iso"])
    if (s.year, s.month, s.day) == (e.year, e.month, e.day):
        return f"{s.day} {MONTHS[s.month]} {s.year}"
    if (s.year, s.month) == (e.year, e.month):
        return f"{s.day}-{e.day} {MONTHS[s.month]} {s.year}"
    if s.year == e.year:
        return f"{s.day} {MONTHS[s.month]} - {e.day} {MONTHS[e.month]} {s.year}"
    return f"{s.day} {MONTHS[s.month]} {s.year} - {e.day} {MONTHS[e.month]} {e.year}"


def human_size(n: int | None) -> str:
    if not n:
        return ""
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("kB", 1 << 10)):
        if n >= div:
            value = n / div
            return f"{value:.1f} {unit}" if value < 10 else f"{value:.0f} {unit}"
    return f"{n} B"


# Indico renderuje opisy Markdownem z rozszerzeniem nl2br - robimy to samo,
# żeby kopia wyglądała jak oryginał.
MD = markdown.Markdown(extensions=["nl2br", "tables", "sane_lists"])
ALLOWED_TAGS = [
    "p", "br", "strong", "em", "code", "pre", "blockquote", "a", "ul", "ol",
    "li", "h4", "h5", "h6", "table", "thead", "tbody", "tr", "th", "td", "hr",
]


def render_markdown(text: str) -> str:
    """Zamienia opis z Indico na HTML; nagłówki degradujemy do h5/h6,
    żeby nie konkurowały ze strukturą strony."""
    if not text:
        return ""
    MD.reset()
    html = MD.convert(text)
    for src, dst in (("h1", "h5"), ("h2", "h5"), ("h3", "h6"), ("h4", "h6")):
        html = html.replace(f"<{src}>", f"<{dst}>").replace(f"</{src}>", f"</{dst}>")
    if bleach is not None:
        html = bleach.clean(html, tags=ALLOWED_TAGS,
                            attributes={"a": ["href", "title", "rel"]}, strip=True)
    return html


def plural(n: int, one: str, few: str, many: str) -> str:
    """Polska odmiana liczebnika: 1 plik / 2-4 pliki / 5+ plików."""
    if n == 1:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and n % 100 not in range(12, 15):
        return f"{n} {few}"
    return f"{n} {many}"


def materials_label(n: int) -> str:
    return "brak materiałów" if n == 0 else plural(n, "materiał", "materiały", "materiałów")


def person_name(participant: dict) -> str:
    first = (participant.get("first_name") or "").strip()
    last = (participant.get("last_name") or "").strip()
    name = f"{first} {last}".strip()
    return name or (participant.get("fullName") or "").strip()


def kind_of(attachment: dict) -> str:
    if attachment.get("type") == "link":
        return "link"
    ct = attachment.get("content_type") or ""
    name = (attachment.get("filename") or "").lower()
    if ct.startswith("video/"):
        return "video"
    if ct == "application/pdf":
        return "pdf"
    if ct.startswith("image/"):
        return "image"
    if "ipynb" in ct or name.endswith(".ipynb"):
        return "notebook"
    if "python" in ct or name.endswith(".py"):
        return "code"
    return "file"


def kind_of_name(name: str) -> str:
    """Odpowiednik kind_of() dla plików, które nie przyszły z Indico."""
    name = name.lower()
    for suffix, kind in ((".pdf", "pdf"), (".ipynb", "notebook"), (".py", "code"),
                         (".qasm", "code"), (".zip", "file"), (".md", "file"),
                         (".png", "image"), (".jpg", "image"), (".svg", "image")):
        if name.endswith(suffix):
            return kind
    return "file"


def fetch_event(event_id: int) -> dict:
    url = f"{BASE}/export/event/{event_id}.json?detail=subcontributions"
    resp = session.get(url, timeout=120)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("results"):
        raise SystemExit(f"Wydarzenie {event_id}: brak danych (dostęp ograniczony?)")
    return payload["results"][0]


class Mirror:
    """Pobiera pliki lokalnie, deduplikując po sumie kontrolnej."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.by_checksum: dict[str, str] = {}
        self.downloaded = 0
        self.reused = 0
        self.skipped: list[tuple[str, str]] = []
        self.skipped_urls: set[str] = set()
        self.bytes = 0

    def path_for(self, attachment: dict) -> str | None:
        kind = kind_of(attachment)
        if kind == "link":
            return None
        ct = attachment.get("content_type") or ""
        size = attachment.get("size") or 0
        if ct.startswith(NO_MIRROR_PREFIXES) or size > MIRROR_MAX_BYTES:
            self.skipped.append((attachment.get("title", "?"), human_size(size)))
            self.skipped_urls.add(attachment["download_url"])
            return None

        checksum = attachment.get("checksum") or str(attachment["id"])
        if checksum in self.by_checksum:
            self.reused += 1
            return self.by_checksum[checksum]

        rel = f"assets/materials/{checksum[:8]}/{safe_filename(attachment['filename'])}"
        target = ROOT / rel
        if self.enabled and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with session.get(attachment["download_url"], stream=True, timeout=300) as resp:
                resp.raise_for_status()
                tmp = target.with_suffix(target.suffix + ".part")
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(1 << 16):
                        fh.write(chunk)
                tmp.replace(target)
            self.downloaded += 1
            self.bytes += target.stat().st_size
            print(f"    ↓ {rel} ({human_size(size)})")
        elif target.exists():
            self.bytes += target.stat().st_size
        self.by_checksum[checksum] = rel
        return rel


class Uzupelnienia:
    """Dokłada do wydarzeń materiały z repozytorium projektu.

    Część warsztatów stacjonarnych nie ma w Indico żadnych załączników —
    w takim wypadku archiwum publikowałoby stronę z programem i podpisem
    „brak materiałów”. Dopóki załączniki nie zostaną dodane w Indico,
    kopiujemy tu odpowiadające im pliki wprost z repozytorium materiałów
    (slajdy PDF) oraz pakujemy notatniki z ćwiczeniami w jedno archiwum ZIP.

    Konfiguracja: tools/materialy_lokalne.json. Po uzupełnieniu Indico
    wystarczy usunąć z niej wpis danego wydarzenia — kopia zniknie ze strony
    przy najbliższym odświeżeniu.
    """

    def __init__(self, enabled: bool, plik: Path = SUPPLEMENT_FILE) -> None:
        self.enabled = enabled and plik.exists()
        self.by_digest: dict[str, str] = {}
        self.dodane: dict[int, int] = {}
        self.bytes = 0
        self.brakujace: list[str] = []
        if not self.enabled:
            self.config: dict = {}
            self.zrodlo = None
            return
        self.config = json.loads(plik.read_text(encoding="utf-8"))
        self.zrodlo = (ROOT / self.config["katalog_zrodlowy"]).resolve()
        self.limit = int(self.config.get("limit_pliku_mb", 60)) * 1024 * 1024
        self.pomijane = self.config.get("pomijane_wzorce", [])
        self.tytuly = self.config.get("tytuly", {})
        if not self.zrodlo.is_dir():
            print(f"Uwaga: brak katalogu z materiałami ({self.zrodlo}) — "
                  f"pomijam uzupełnienia z repozytorium.")
            self.enabled = False

    # -- pomocnicze ------------------------------------------------------- #

    def _pomijany(self, sciezka: Path) -> bool:
        rel = sciezka.relative_to(self.zrodlo).as_posix()
        return any(fnmatch.fnmatch(rel, wzorzec) for wzorzec in self.pomijane)

    def _rozwin(self, wzorce: list[str]) -> list[Path]:
        """Zamienia listę ścieżek i wzorców glob na posortowaną listę plików."""
        znalezione: list[Path] = []
        for wzorzec in wzorce:
            trafienia = sorted(self.zrodlo.glob(wzorzec))
            if not trafienia:
                self.brakujace.append(wzorzec)
            for sciezka in trafienia:
                if sciezka.is_dir():
                    znalezione.extend(sorted(q for q in sciezka.rglob("*") if q.is_file()))
                elif sciezka.is_file():
                    znalezione.append(sciezka)
        wynik, widziane = [], set()
        for sciezka in znalezione:
            if sciezka in widziane or self._pomijany(sciezka):
                continue
            widziane.add(sciezka)
            wynik.append(sciezka)
        return wynik

    @staticmethod
    def _md5(sciezka: Path) -> str:
        suma = hashlib.md5()
        with open(sciezka, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                suma.update(chunk)
        return suma.hexdigest()

    def _opublikuj(self, sciezka: Path, digest: str, nazwa: str) -> str:
        """Kopiuje plik do assets/materials/, deduplikując po sumie MD5."""
        if digest in self.by_digest:
            return self.by_digest[digest]
        rel = f"assets/materials/{digest[:8]}/{safe_filename(nazwa)}"
        target = ROOT / rel
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".part")
            shutil.copyfile(sciezka, tmp)
            tmp.replace(target)
            print(f"    + {rel} ({human_size(target.stat().st_size)})")
        self.bytes += target.stat().st_size
        self.by_digest[digest] = rel
        return rel

    def _pozycja_pliku(self, sciezka: Path) -> dict | None:
        rozmiar = sciezka.stat().st_size
        if rozmiar > self.limit:
            print(f"    ! pomijam {sciezka.name} ({human_size(rozmiar)} > limit)")
            return None
        rel = self._opublikuj(sciezka, self._md5(sciezka), sciezka.name)
        return {
            "title": self.tytuly.get(sciezka.name, sciezka.name),
            "kind": kind_of_name(sciezka.name),
            "size": rozmiar,
            "size_label": human_size(rozmiar),
            "local": rel,
            "remote": "",
            "source": "",
            "filename": sciezka.name,
        }

    def _pozycja_paczki(self, paczka: dict) -> dict | None:
        """Buduje archiwum ZIP z wzorców; zip jest deterministyczny (stałe
        znaczniki czasu), więc ta sama zawartość daje tę samą sumę MD5
        i nie tworzy nowego katalogu w assets/materials/ przy każdym odświeżeniu.

        Duże zbiory danych pomijamy (limit_pliku_mb), żeby archiwum dało się
        pobrać jednym kliknięciem; w środku zostaje spis pominiętych plików
        wraz z adresem repozytorium, z którego można je wziąć."""
        pliki = self._rozwin(paczka["wzorce"])
        if not pliki:
            return None
        limit_wpisu = int(paczka.get("limit_pliku_mb", 0)) * 1024 * 1024
        pominiete: list[Path] = []
        if limit_wpisu:
            pliki, pominiete = ([q for q in pliki if q.stat().st_size <= limit_wpisu],
                                [q for q in pliki if q.stat().st_size > limit_wpisu])

        bufor = ROOT / ".cache" / paczka["nazwa"]
        bufor.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(bufor, "w", zipfile.ZIP_DEFLATED) as archiwum:
            for sciezka in pliki:
                info = zipfile.ZipInfo(sciezka.relative_to(self.zrodlo).as_posix(),
                                       date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archiwum.writestr(info, sciezka.read_bytes())
            if pominiete:
                info = zipfile.ZipInfo("DANE-POBIERANE-OSOBNO.txt",
                                       date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = 0o644 << 16
                archiwum.writestr(info, self._nota_o_danych(pominiete))

        rozmiar = bufor.stat().st_size
        if rozmiar > self.limit:
            print(f"    ! pomijam archiwum {paczka['nazwa']} "
                  f"({human_size(rozmiar)} > limit {human_size(self.limit)})")
            bufor.unlink()
            return None
        rel = self._opublikuj(bufor, self._md5(bufor), paczka["nazwa"])
        bufor.unlink()
        return {
            "title": paczka.get("tytul") or paczka["nazwa"],
            "kind": "file",
            "size": rozmiar,
            "size_label": human_size(rozmiar),
            "local": rel,
            "remote": "",
            "source": "",
            "filename": paczka["nazwa"],
            "n_plikow": len(pliki),
            "n_pominietych": len(pominiete),
        }

    def _nota_o_danych(self, pominiete: list[Path]) -> str:
        wiersze = [
            "Duże pliki z danymi pominięto w tym archiwum, aby zachować",
            "rozsądny rozmiar pobierania. Wszystkie znajdują się w repozytorium",
            "materiałów projektu:",
            "",
            f"  {self.config['repozytorium']}",
            "",
            "Pominięte pliki:",
            "",
        ]
        wiersze += [f"  {q.relative_to(self.zrodlo).as_posix()}  "
                    f"({human_size(q.stat().st_size)})" for q in pominiete]
        return "\n".join(wiersze) + "\n"

    # -- interfejs -------------------------------------------------------- #

    def folders_for(self, event_id: int) -> list[dict]:
        if not self.enabled:
            return []
        nazwa = self.config.get("wydarzenia", {}).get(str(event_id))
        if not nazwa:
            return []
        zestaw = self.config["zestawy"][nazwa]

        items = [poz for sciezka in self._rozwin(zestaw.get("pliki", []))
                 if (poz := self._pozycja_pliku(sciezka))]
        if paczka := zestaw.get("paczka"):
            if poz := self._pozycja_paczki(paczka):
                items.append(poz)
        if not items:
            return []

        strona = zestaw.get("strona_bloku")
        if strona:
            items.append({
                "title": "Wszystkie materiały bloku wraz z podglądem notatników "
                         "w przeglądarce",
                "kind": "link",
                "size": None,
                "size_label": "",
                "local": None,
                "remote": self.config["strona_materialow"].rstrip("/") + strona,
                "source": "strona materiałów",
                "filename": None,
            })

        self.dodane[event_id] = len(items)
        czesci = [zestaw.get("opis", "").strip(),
                  f"Pliki skopiowane z repozytorium materiałów projektu "
                  f"({self.config['repozytorium']})."]
        return [{
            "title": zestaw.get("tytul") or "Materiały warsztatów",
            "description": " ".join(c for c in czesci if c),
            "items": items,
        }]

    def podsumowanie(self) -> str:
        if not self.enabled or not self.dodane:
            return ""
        return (f"Uzupełnienia z repozytorium: {len(self.dodane)} wydarzeń, "
                f"{len(self.by_digest)} unikalnych plików, {human_size(self.bytes)}")


def load_pomijane() -> dict:
    """Wzorce załączników, których archiwum nie republikuje."""
    if not SKIP_FILE.exists():
        return {"url": [], "tytul": []}
    cfg = json.loads(SKIP_FILE.read_text(encoding="utf-8"))
    return {
        "url": [w.lower() for w in cfg.get("wzorce_url", [])],
        "tytul": [w.lower() for w in cfg.get("wzorce_tytulu", [])],
    }


POMIJANE = load_pomijane()
POMINIETE: list[str] = []


def pomijany_zalacznik(att: dict) -> bool:
    adres = ((att.get("link_url") or "") + " " + (att.get("download_url") or "")).lower()
    tytul = (att.get("title") or att.get("filename") or "").lower()
    if any(w in adres for w in POMIJANE["url"]) or any(w in tytul for w in POMIJANE["tytul"]):
        POMINIETE.append(att.get("title") or att.get("filename") or adres.strip())
        return True
    return False


def build_folders(raw_folders: list[dict] | None, mirror: Mirror) -> list[dict]:
    folders = []
    for folder in raw_folders or []:
        items = []
        for att in folder.get("attachments", []):
            if pomijany_zalacznik(att):
                continue
            kind = kind_of(att)
            local = mirror.path_for(att)
            remote = att.get("link_url") or att.get("download_url")
            source = ""
            if not local and kind != "link":
                moved = VIDEO_LINKS["files"].get(att.get("checksum") or "")
                if moved:
                    remote = moved["url"]
                    source = VIDEO_LINKS["source_label"]
                else:
                    source = "Indico"
            items.append({
                "title": att.get("title") or att.get("filename") or "materiał",
                "kind": kind,
                "size": att.get("size"),
                "size_label": human_size(att.get("size")),
                "local": local,
                "remote": remote,
                "source": source,
                "filename": att.get("filename"),
            })
        if not items:
            continue
        items.sort(key=lambda i: (i["kind"] == "video", i["title"].lower()))
        folders.append({
            "title": folder.get("title") or "",
            "description": (folder.get("description") or "").strip(),
            "items": items,
        })
    folders.sort(key=lambda f: (f["title"] != "", f["title"].lower()))
    return folders


def clean_text(text: str | None) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def build_event(event_id: int, kind: str, mirror: Mirror,
                uzupelnienia: Uzupelnienia | None = None) -> dict:
    raw = fetch_event(event_id)
    print(f"  • [{event_id}] {raw['title']}")
    start = to_local(raw["startDate"])
    end = to_local(raw["endDate"])

    place_parts = [p.strip() for p in (raw.get("location"), raw.get("room"), raw.get("address")) if (p or "").strip()]
    place = " • ".join(clean_text(p).replace("\n", ", ") for p in place_parts)

    contributions = []
    for c in raw.get("contributions", []):
        c_start, c_end = to_local(c["startDate"]), to_local(c["endDate"])
        contributions.append({
            "id": c.get("friendly_id"),
            "title": clean_text(c.get("title")),
            "start": c_start,
            "end": c_end,
            "duration": c.get("duration"),
            "room": clean_text(c.get("room")),
            "speakers": [person_name(s) for s in (c.get("speakers") or [])],
            "description_html": render_markdown(clean_text(c.get("description"))),
            "folders": build_folders(c.get("folders"), mirror),
        })
    contributions.sort(key=lambda c: (c["start"]["iso"] if c["start"] else "", str(c["id"])))

    days: list[dict] = []
    for c in contributions:
        label = c["start"]["day_label"] if c["start"] else "Termin nieokreślony"
        if not days or days[-1]["label"] != label:
            days.append({"label": label, "contributions": []})
        days[-1]["contributions"].append(c)

    event_folders = build_folders(raw.get("folders"), mirror)
    if uzupelnienia:
        event_folders.extend(uzupelnienia.folders_for(event_id))
    n_files = sum(len(f["items"]) for f in event_folders)
    n_files += sum(len(f["items"]) for c in contributions for f in c["folders"])

    return {
        "id": int(event_id),
        "slug": SLUGS.get(event_id) or slugify(raw["title"]),
        "kind": kind,
        "title": clean_text(raw["title"]),
        "start": start,
        "end": end,
        "date_label": date_range_label(start, end),
        "place": place,
        "location": clean_text(raw.get("location")),
        "room": clean_text(raw.get("room")),
        "address": clean_text(raw.get("address")),
        "indico_url": raw.get("url") or f"{BASE}/event/{event_id}/",
        "folders": event_folders,
        "days": days,
        "n_materials": n_files,
        "n_materials_label": materials_label(n_files),
    }


PAGE_TEMPLATE = """---
layout: default
bg: "{bg}"
permalink: /archiwum/{slug}/
title: "{title}"
summary: "{summary}"
event_id: {event_id}
active: false
---
{{% assign event = nil %}}
{{% for candidate in site.data.archiwum.events %}}
  {{% if candidate.id == page.event_id %}}{{% assign event = candidate %}}{{% endif %}}
{{% endfor %}}
{{% include event.html event=event %}}
"""


def write_pages(events: list[dict]) -> None:
    PAGES_DIR.mkdir(exist_ok=True)
    known = set()
    for ev in events:
        bg = "main.jpg" if ev["kind"] == "online" else "trainings.jpg"
        path = PAGES_DIR / f"{ev['slug']}.html"
        summary = f"{ev['date_label']}" + (f" • {ev['place']}" if ev["place"] else "")
        path.write_text(PAGE_TEMPLATE.format(
            bg=bg,
            slug=ev["slug"],
            title=ev["title"].replace('"', "'"),
            summary=summary.replace('"', "'"),
            event_id=ev["id"],
        ), encoding="utf-8")
        known.add(path.name)
    for stale in PAGES_DIR.glob("*.html"):
        if stale.name not in known:
            print(f"  ! usuwam nieaktualną stronę {stale.name}")
            stale.unlink()


def usun_osierocone(events: list[dict]) -> None:
    """Usuwa z assets/materials/ pliki, do których nic już nie linkuje.

    Wywoływane tylko po pełnym odświeżeniu (bez --no-download i bez
    --bez-uzupelnien), bo tylko wtedy zestaw odnośników jest kompletny."""
    uzyte: set[str] = set()

    def zbierz(folders: list[dict]) -> None:
        for folder in folders:
            for item in folder["items"]:
                if item.get("local"):
                    uzyte.add(item["local"])

    for ev in events:
        zbierz(ev["folders"])
        for day in ev["days"]:
            for c in day["contributions"]:
                zbierz(c["folders"])

    for plik in sorted(MATERIALS_DIR.rglob("*")):
        if not plik.is_file():
            continue
        if plik.relative_to(ROOT).as_posix() in uzyte:
            continue
        print(f"  ! usuwam nieużywany plik {plik.relative_to(ROOT)} "
              f"({human_size(plik.stat().st_size)})")
        plik.unlink()
    for katalog in sorted(MATERIALS_DIR.iterdir()):
        if katalog.is_dir() and not any(katalog.iterdir()):
            katalog.rmdir()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-download", action="store_true",
                        help="nie pobieraj plików, odśwież tylko metadane")
    parser.add_argument("--bez-uzupelnien", action="store_true",
                        help="nie dokładaj materiałów z repozytorium projektu "
                             "(patrz tools/materialy_lokalne.json)")
    args = parser.parse_args()

    _relax_tls_if_needed()
    mirror = Mirror(enabled=not args.no_download)
    uzupelnienia = Uzupelnienia(enabled=not args.bez_uzupelnien)
    events: list[dict] = []

    print("Wykłady online:")
    for eid in ONLINE_IDS:
        events.append(build_event(eid, "online", mirror, uzupelnienia))
    print("Warsztaty stacjonarne:")
    for eid in WORKSHOP_IDS:
        events.append(build_event(eid, "warsztaty", mirror, uzupelnienia))

    events.sort(key=lambda e: e["start"]["iso"])

    now = datetime.now(TZ)
    n_online = sum(1 for e in events if e["kind"] == "online")
    n_warsztaty = len(events) - n_online
    n_remote = len({a for a in mirror.skipped_urls})
    data = {
        "source": BASE,
        "fetched_at": now.strftime("%Y-%m-%d"),
        "fetched_at_label": f"{now.day} {MONTHS[now.month]} {now.year}",
        "video_host": {
            "label": VIDEO_LINKS["source_label"],
            "record_url": VIDEO_LINKS.get("record_url") or "",
            "doi": VIDEO_LINKS.get("doi"),
            "moved": bool(VIDEO_LINKS["files"]),
            "streamable": bool(VIDEO_LINKS.get("streamable")),
        },
        "stats": {
            "events": plural(len(events), "wydarzenie", "wydarzenia", "wydarzeń"),
            "online": plural(n_online, "wydarzenie online", "wydarzenia online",
                             "wydarzeń online"),
            "warsztaty": plural(n_warsztaty, "warsztat stacjonarny",
                                "warsztaty stacjonarne", "warsztatów stacjonarnych"),
            "local_files": plural(len(mirror.by_checksum), "plik skopiowany lokalnie",
                                  "pliki skopiowane lokalnie", "plików skopiowanych lokalnie"),
            "remote_files": plural(n_remote, "plik pozostawiony w Indico",
                                   "pliki pozostawione w Indico",
                                   "plików pozostawionych w Indico"),
            "local_bytes": human_size(mirror.bytes),
        },
        "events": events,
    }
    DATA_FILE.parent.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    write_pages(events)
    if not args.no_download and not args.bez_uzupelnien:
        usun_osierocone(events)

    n_local = len(mirror.by_checksum)
    print(f"\nZapisano {DATA_FILE.relative_to(ROOT)}: {len(events)} wydarzeń")
    print(f"Materiały lokalne: {n_local} unikalnych plików, {human_size(mirror.bytes)}"
          f" (nowo pobrane: {mirror.downloaded}, użyte ponownie: {mirror.reused})")
    if VIDEO_LINKS["files"]:
        print(f"Nagrania linkowane do {VIDEO_LINKS['source_label']}: "
              f"{len(VIDEO_LINKS['files'])} unikalnych plików "
              f"({len(mirror.skipped)} wpisów na stronach), "
              f"rekord {VIDEO_LINKS.get('record_url')}")
    else:
        print(f"Pozostawione jako linki do Indico: {len(mirror.skipped)} plików")
    if POMINIETE:
        print(f"Pominięte załączniki (tools/pomijane_zalaczniki.json): {len(POMINIETE)}")
        for tytul in POMINIETE:
            print(f"  - {tytul}")
    if podsumowanie := uzupelnienia.podsumowanie():
        print(podsumowanie)
    if uzupelnienia.brakujace:
        print("Uwaga: wzorce bez trafień w katalogu materiałów: "
              + ", ".join(sorted(set(uzupelnienia.brakujace))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
