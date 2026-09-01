---
bg: "trainings.jpg"
layout: default
permalink: /archiwum/
title: "Archiwum wydarzeń"
nav_title: "Archiwum"
summary: "Wykłady online i warsztaty — program i materiały"
active: true
---

# Archiwum wydarzeń

Poniżej znajduje się pełna kopia danych o **wykładach online** i **warsztatach
stacjonarnych** zrealizowanych w ramach projektu „Akademia Sztuki Kwantowej”:
tytuł, terminy, miejsce, program oraz wszystkie materiały dołączone do każdego
wydarzenia.

Dane pochodzą z systemu Indico ([{{ site.data.archiwum.source | remove: 'https://' }}]({{ site.data.archiwum.source }})),
stan na **{{ site.data.archiwum.fetched_at_label }}**. Prezentacje, notatniki i skrypty
zostały skopiowane na ten serwer i są dostępne bezpośrednio.
{% assign host = site.data.archiwum.video_host %}
{% if host.moved %}Nagrania wykładów są udostępniane z {{ host.label }}{% if host.doi %}
([DOI: {{ host.doi }}](https://doi.org/{{ host.doi }})){% endif %} — każde nagranie
ma osobny odnośnik przy odpowiednim punkcie programu.{% else %}Nagrania wideo pozostają
na serwerze Indico — przy nich podano odnośnik do oryginału.{% endif %}

{% assign online = site.data.archiwum.events | where: 'kind', 'online' %}
{% assign warsztaty = site.data.archiwum.events | where: 'kind', 'warsztaty' %}

<p class="arch-summary">
  {{ site.data.archiwum.stats.events }} ·
  {{ site.data.archiwum.stats.online }} ·
  {{ site.data.archiwum.stats.warsztaty }}<br>
  {{ site.data.archiwum.stats.local_files }} ({{ site.data.archiwum.stats.local_bytes }}) ·
  {{ site.data.archiwum.stats.remote_files }}
</p>

## Wykłady online

<div class="arch-list">
{% for event in online %}
  <a class="arch-card" href="{{ '/archiwum/' | append: event.slug | append: '/' | relative_url }}">
    <span class="arch-card-date">{{ event.date_label }}</span>
    <span class="arch-card-title">{{ event.title }}</span>
    <span class="arch-card-meta">
      {% if event.place != '' %}{{ event.place }} &middot; {% endif %}{{ event.n_materials_label }}
    </span>
  </a>
{% endfor %}
</div>

## Warsztaty stacjonarne

<div class="arch-list">
{% for event in warsztaty %}
  <a class="arch-card" href="{{ '/archiwum/' | append: event.slug | append: '/' | relative_url }}">
    <span class="arch-card-date">{{ event.date_label }}</span>
    <span class="arch-card-title">{{ event.title }}</span>
    <span class="arch-card-meta">
      {% if event.place != '' %}{{ event.place }} &middot; {% endif %}{{ event.n_materials_label }}
    </span>
  </a>
{% endfor %}
</div>

## O tej kopii

Kopia została wygenerowana skryptem `tools/fetch_indico.py` z publicznego API
eksportu Indico. Skrypt można uruchomić ponownie, aby odświeżyć dane:

```
python3 tools/fetch_indico.py
```

Kopia **nie obejmuje** konsultacji prowadzonych w ramach projektu
„Zaawansowana optymalizacja w służbie niezawodnego i efektywnego transportu
publicznego”, które są odrębnym przedsięwzięciem.
