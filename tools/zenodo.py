#!/usr/bin/env python3
"""Przenosi nagrania wykładów z Indico do rekordu na Zenodo.

Nagrania (ok. 15 GB) nie mieszczą się w limitach GitHub Pages, więc trafiają
do jednego rekordu na Zenodo, a strona linkuje do poszczególnych plików.

Etapy:
    python3 tools/zenodo.py download KATALOG
        Pobiera unikalne nagrania z Indico (deduplikacja po MD5) i weryfikuje
        sumy kontrolne. Idempotentne - powtórne uruchomienie nic nie pobiera.

    python3 tools/zenodo.py upload KATALOG [--sandbox] [--record ID]
        Tworzy szkic rekordu (lub uzupełnia istniejący) i wgrywa pliki.
        Szkic NIE jest publikowany - publikacja to osobna, świadoma decyzja.

    python3 tools/zenodo.py publish --record ID [--sandbox]
        Publikuje szkic. Nieodwracalne: plików nie da się potem usunąć.

    python3 tools/zenodo.py links --record ID [--sandbox]
        Zapisuje _data/video_urls.json (MD5 -> URL pliku na Zenodo).
        Po tym kroku `fetch_indico.py` linkuje nagrania do Zenodo.
        Korzysta z tools/video_manifest.json, więc odbudowa linków po nowej
        wersji rekordu nie wymaga ponownego pobierania nagrań.

Token API czytany jest w tej kolejności:
    1. plik ~/.config/zenodo/token (zalecane - nie trafia do listy procesów),
    2. zmienna środowiskowa ZENODO_TOKEN,
    3. --token (widoczne w `ps`, używaj tylko doraźnie).
Uprawnienia tokenu: deposit:write oraz deposit:actions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_indico as ind  # noqa: E402  (wspólne stałe, sesja HTTP, helpery)

ROOT = ind.ROOT
LINKS_FILE = ROOT / "_data" / "video_urls.json"

LIVE = "https://zenodo.org"
SANDBOX = "https://sandbox.zenodo.org"
TOKEN_FILE = Path.home() / ".config" / "zenodo" / "token"
TOKEN_FILE_SANDBOX = Path.home() / ".config" / "zenodo" / "token-sandbox"


# --------------------------------------------------------------------------- #
# etap 1: pobranie nagrań z Indico
# --------------------------------------------------------------------------- #

def collect_videos() -> list[dict]:
    """Zwraca listę unikalnych nagrań (po MD5) ze wszystkich wydarzeń."""
    ind._relax_tls_if_needed()
    unique: dict[str, dict] = {}
    for event_id in ind.ONLINE_IDS + ind.WORKSHOP_IDS:
        raw = ind.fetch_event(event_id)

        def walk(folders):
            for folder in folders or []:
                for att in folder.get("attachments", []):
                    if (att.get("content_type") or "") != "video/mp4":
                        continue
                    checksum = att.get("checksum")
                    if not checksum or checksum in unique:
                        continue
                    unique[checksum] = {
                        "checksum": checksum,
                        "title": att.get("title") or att["filename"],
                        "size": att.get("size") or 0,
                        "url": att["download_url"],
                        "event": event_id,
                    }

        walk(raw.get("folders"))
        for contrib in raw.get("contributions", []):
            walk(contrib.get("folders"))

    videos = sorted(unique.values(), key=lambda v: v["title"].lower())
    used: set[str] = set()
    for video in videos:
        stem = ind.safe_filename(video["title"]).rsplit(".", 1)[0][:90]
        name = f"{stem}.mp4"
        n = 2
        while name.lower() in used:
            name = f"{stem}-{n}.mp4"
            n += 1
        used.add(name.lower())
        video["filename"] = name
    return videos


def md5_of(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wait_for_server(max_wait: int = 3600) -> bool:
    """Indico bywa niedostępne. Czekamy, aż wróci, zamiast przerywać pracę."""
    import requests

    waited, delay = 0, 15
    while waited < max_wait:
        try:
            ind.session.get(f"{ind.BASE}/", timeout=20).raise_for_status()
            if waited:
                print(f"    serwer wrócił po {waited}s", flush=True)
            return True
        except requests.exceptions.RequestException:
            print(f"    Indico nie odpowiada - czekam {delay}s "
                  f"(łącznie {waited}s)", flush=True)
            time.sleep(delay)
            waited += delay
            delay = min(120, delay * 2)
    return False


def download_one(video: dict, path: Path, attempts: int = 8) -> bool:
    """Pobiera jeden plik, wznawiając po zerwaniu połączenia.

    Indico zrywa transfer na dużych plikach, więc próbujemy wznowić od miejsca
    przerwania nagłówkiem Range; jeśli serwer go zignoruje, zaczynamy od zera.
    """
    import requests

    tmp = path.with_suffix(".mp4.part")
    size = video["size"]

    for attempt in range(1, attempts + 1):
        have = tmp.stat().st_size if tmp.exists() else 0
        if have > size:            # śmieci z poprzedniej próby
            tmp.unlink()
            have = 0
        if have == size:
            break

        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with ind.session.get(video["url"], stream=True, timeout=(30, 300),
                                 headers=headers) as resp:
                if have and resp.status_code == 206:
                    mode = "ab"
                elif resp.status_code == 200:
                    have, mode = 0, "wb"   # serwer nie wspiera wznawiania
                elif resp.status_code == 416:
                    break                  # mamy już całość
                else:
                    resp.raise_for_status()
                    mode = "wb"
                with tmp.open(mode) as fh:
                    for chunk in resp.iter_content(1 << 20):
                        fh.write(chunk)
        except requests.exceptions.RequestException as exc:
            got = tmp.stat().st_size if tmp.exists() else 0
            kind = type(exc).__name__
            print(f"    próba {attempt}/{attempts}: {kind}, "
                  f"mam {got/2**20:.0f}/{size/2**20:.0f} MiB", flush=True)
            if isinstance(exc, requests.exceptions.ConnectionError) and got == have:
                if not wait_for_server():
                    return False
            else:
                time.sleep(min(60, 3 * attempt))
            continue

        if tmp.exists() and tmp.stat().st_size == size:
            break
        print(f"    próba {attempt}/{attempts}: transfer urwany na "
              f"{tmp.stat().st_size/2**20:.0f}/{size/2**20:.0f} MiB", flush=True)
        time.sleep(min(30, 2 * attempt))
    else:
        return False

    actual = md5_of(tmp)
    if actual != video["checksum"]:
        print(f"    BŁĄD MD5: {actual[:12]} != {video['checksum'][:12]}", flush=True)
        tmp.unlink()
        return False
    tmp.replace(path)
    return True


def cmd_download(args: argparse.Namespace) -> int:
    import requests

    target = Path(args.dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    manifest = target / "manifest.json"

    # Lista nagrań pochodzi z Indico, ale gdy serwer leży, a manifest już mamy,
    # wznawiamy pobieranie zamiast przerywać pracę.
    try:
        videos = collect_videos()
        manifest.write_text(json.dumps(videos, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    except (requests.exceptions.RequestException, SystemExit) as exc:
        if not manifest.exists():
            raise SystemExit(f"Indico nieosiągalne i brak manifestu: {exc}")
        videos = json.loads(manifest.read_text(encoding="utf-8"))
        print(f"Indico nie odpowiada - używam zapisanego manifestu "
              f"({len(videos)} nagrań).", flush=True)

    total = sum(v["size"] for v in videos)
    print(f"{len(videos)} unikalnych nagrań, razem {ind.human_size(total)}\n",
          flush=True)

    done_bytes = 0
    for i, video in enumerate(videos, 1):
        path = target / video["filename"]
        label = f"[{i:2}/{len(videos)}] {video['filename'][:64]:64}"
        if path.exists() and path.stat().st_size == video["size"]:
            if md5_of(path) == video["checksum"]:
                done_bytes += video["size"]
                print(f"{label} jest już ({ind.human_size(video['size'])})", flush=True)
                continue
            print(f"{label} zła suma MD5 - pobieram ponownie")

        print(f"{label} pobieram {ind.human_size(video['size']):>9}", flush=True)
        if not download_one(video, path):
            print(f"\n{label} NIE UDAŁO SIĘ po wszystkich próbach.")
            print("Uruchom polecenie ponownie, gdy Indico wróci "
                  "- pobrane pliki zostaną pominięte.")
            return 1
        done_bytes += video["size"]
        print(f"{label} OK  {ind.human_size(video['size']):>9}"
              f"   razem {ind.human_size(done_bytes)}/{ind.human_size(total)}", flush=True)

    print(f"\nGotowe: {len(videos)} plików, {ind.human_size(done_bytes)}, "
          f"wszystkie sumy MD5 zgodne z Indico.")
    return 0


# --------------------------------------------------------------------------- #
# etap 2: rekord na Zenodo
# --------------------------------------------------------------------------- #

def read_token(args: argparse.Namespace) -> str:
    path = Path(args.token_file) if args.token_file else (
        TOKEN_FILE_SANDBOX if args.sandbox else TOKEN_FILE)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    if os.environ.get("ZENODO_TOKEN"):
        return os.environ["ZENODO_TOKEN"].strip()
    if args.token:
        return args.token.strip()
    raise SystemExit(
        f"Brak tokenu. Zapisz go w {path} (chmod 600) albo ustaw ZENODO_TOKEN.")


def api(args: argparse.Namespace) -> tuple[str, dict]:
    base = SANDBOX if args.sandbox else LIVE
    return base, {"Authorization": f"Bearer {read_token(args)}"}


def record_metadata() -> dict:
    """Metadane rekordu. Twórcy i licencja - patrz README w tools/."""
    meta_path = Path(__file__).with_name("zenodo_metadata.json")
    if not meta_path.exists():
        raise SystemExit(f"Brak pliku {meta_path.name} - opisz w nim rekord.")
    return json.loads(meta_path.read_text(encoding="utf-8"))


def cmd_upload(args: argparse.Namespace) -> int:
    import requests

    base, headers = api(args)
    target = Path(args.dir).expanduser().resolve()
    videos = json.loads((target / "manifest.json").read_text(encoding="utf-8"))

    if args.record:
        dep = requests.get(f"{base}/api/deposit/depositions/{args.record}",
                           headers=headers, timeout=60)
        dep.raise_for_status()
        deposition = dep.json()
        print(f"Uzupełniam istniejący szkic {args.record}")
    else:
        dep = requests.post(f"{base}/api/deposit/depositions", json={},
                            headers=headers, timeout=60)
        dep.raise_for_status()
        deposition = dep.json()
        print(f"Utworzono szkic {deposition['id']}")

    bucket = deposition["links"]["bucket"]
    already = {f["filename"] for f in deposition.get("files", [])}

    for i, video in enumerate(videos, 1):
        name = video["filename"]
        path = target / name
        label = f"[{i:2}/{len(videos)}] {name[:60]:60}"
        if name in already:
            print(f"{label} już wgrane")
            continue
        if not path.exists():
            print(f"{label} BRAK PLIKU - najpierw `download`")
            return 1
        with path.open("rb") as fh:
            put = requests.put(f"{bucket}/{name}", data=fh, headers=headers,
                               timeout=3600)
        if put.status_code not in (200, 201):
            print(f"{label} BŁĄD {put.status_code}: {put.text[:200]}")
            return 1
        print(f"{label} wgrane {ind.human_size(video['size']):>9}")

    meta = requests.put(f"{base}/api/deposit/depositions/{deposition['id']}",
                        json={"metadata": record_metadata()},
                        headers=headers, timeout=60)
    if meta.status_code != 200:
        print(f"BŁĄD metadanych {meta.status_code}: {meta.text[:400]}")
        return 1

    print(f"\nSzkic gotowy i NIEOPUBLIKOWANY: {base}/uploads/{deposition['id']}")
    print(f"Sprawdź go, a potem: python3 tools/zenodo.py publish "
          f"--record {deposition['id']}{' --sandbox' if args.sandbox else ''}")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    import requests

    base, headers = api(args)
    resp = requests.post(
        f"{base}/api/deposit/depositions/{args.record}/actions/publish",
        headers=headers, timeout=120)
    if resp.status_code != 202:
        print(f"BŁĄD {resp.status_code}: {resp.text[:400]}")
        return 1
    published = resp.json()
    print(f"Opublikowano: {published['links']['record_html']}")
    print(f"DOI: {published.get('doi')}")
    return 0


def cmd_links(args: argparse.Namespace) -> int:
    import requests

    base, headers = api(args)
    resp = requests.get(f"{base}/api/records/{args.record}",
                        headers=headers, timeout=60)
    resp.raise_for_status()
    record = resp.json()

    by_name = {f["key"]: f for f in record.get("files", [])}
    if args.dir:
        source = Path(args.dir).expanduser() / "manifest.json"
    else:
        source = Path(__file__).with_name("video_manifest.json")
    if not source.exists():
        raise SystemExit(f"Brak manifestu ({source}). Uruchom `download`.")
    manifest = json.loads(source.read_text(encoding="utf-8"))
    print(f"manifest: {source}")

    from urllib.parse import quote

    mapping = {}
    missing = []
    for video in manifest:
        entry = by_name.get(video["filename"])
        if not entry:
            missing.append(video["filename"])
            continue
        mapping[video["checksum"]] = {
            "url": f"{base}/records/{args.record}/files/"
                   f"{quote(video['filename'])}?download=1",
            "filename": video["filename"],
        }
    if missing:
        print(f"UWAGA: brak na Zenodo {len(missing)} plików: {missing[:5]}")

    LINKS_FILE.write_text(json.dumps({
        "record": str(args.record),
        "record_url": f"{base}/records/{args.record}",
        "doi": record.get("doi"),
        "source_label": "Zenodo",
        # Zenodo to archiwum, nie CDN: serwuje pliki jako application/octet-stream
        # z Content-Disposition: attachment, bez faststart i z ~1,4 MB/s.
        # Odtwarzanie w <video> kończy się kręcącym się kółkiem, więc linkujemy
        # do pobrania. Host z poprawnym video/mp4 może to ustawić na true.
        "streamable": False,
        "files": mapping,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Zapisano {LINKS_FILE.relative_to(ROOT)}: {len(mapping)} nagrań")
    print("Teraz uruchom: python3 tools/fetch_indico.py --no-download")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--token", help="token API (ostatnia opcja - widoczny w ps)")
    parser.add_argument("--token-file", help="plik z tokenem "
                        "(domyślnie ~/.config/zenodo/token)")
    parser.add_argument("--sandbox", action="store_true",
                        help="użyj sandbox.zenodo.org do testów")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("download", help="pobierz nagrania z Indico")
    p.add_argument("dir")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("upload", help="wgraj nagrania do szkicu rekordu")
    p.add_argument("dir")
    p.add_argument("--record", help="ID istniejącego szkicu")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("publish", help="opublikuj szkic (nieodwracalne)")
    p.add_argument("--record", required=True)
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("links", help="zapisz mapowanie MD5 -> URL")
    p.add_argument("--record", required=True)
    p.add_argument("--dir", help="katalog z manifest.json "
                   "(domyślnie tools/video_manifest.json)")
    p.set_defaults(func=cmd_links)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
