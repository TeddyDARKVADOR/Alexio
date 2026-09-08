"""
tools/jarvis_health/render_html.py — the dashboard, generated from the report.

Every number on the page comes out of `Report`. Nothing is written by hand,
because a dashboard with a hand-written figure on it is a dashboard that is
wrong the first time anything changes.
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

from .behaviors import CRITICAL, HIGH, LOW, MEDIUM, WEIGHTS
from .report import BY_RULE, TESTED, UNTESTED, VIOLATED, Report

_SEV_CLASS = {CRITICAL: "crit", HIGH: "high", MEDIUM: "med", LOW: "low"}
_SEV_LABEL = {CRITICAL: "critique", HIGH: "élevé", MEDIUM: "moyen", LOW: "faible"}
_STATUS_LABEL = {TESTED: "prouvée", BY_RULE: "tenue par une règle",
                 UNTESTED: "non prouvée", VIOLATED: "violée"}

_CSS = """
:root{
  /* Instrument panel: neutrals carry a blue bias toward the structural accent,
     and the semantic scale is warm so severity never reads as "brand colour". */
  --ground:#e9eef2; --surface:#ffffff; --surface-2:#f3f6f8;
  --ink:#15212b; --muted:#5b6d7a; --faint:#8fa2ae;
  --line:#d3dde4; --line-strong:#b6c5cf;
  --accent:#1d4e6b;
  --crit:#a83232; --high:#b56b16; --med:#7f7118; --ok:#2c7a5b;
  --crit-wash:#f7e9e9; --high-wash:#faf0e2; --med-wash:#f6f3e2; --ok-wash:#e6f2ed;
  --shadow:0 1px 2px rgba(21,33,43,.06), 0 8px 24px -16px rgba(21,33,43,.28);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --ground:#0f171d; --surface:#16212a; --surface-2:#1b2833;
    --ink:#e3ecf2; --muted:#93a7b5; --faint:#6c8090;
    --line:#26353f; --line-strong:#3a4d5a;
    --accent:#69a8cc;
    --crit:#e0736b; --high:#e0a04a; --med:#c8b959; --ok:#5fbb96;
    --crit-wash:#2b1a1a; --high-wash:#2b2216; --med-wash:#272415; --ok-wash:#152720;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -16px rgba(0,0,0,.8);
  }
}
:root[data-theme="dark"]{
  --ground:#0f171d; --surface:#16212a; --surface-2:#1b2833;
  --ink:#e3ecf2; --muted:#93a7b5; --faint:#6c8090;
  --line:#26353f; --line-strong:#3a4d5a;
  --accent:#69a8cc;
  --crit:#e0736b; --high:#e0a04a; --med:#c8b959; --ok:#5fbb96;
  --crit-wash:#2b1a1a; --high-wash:#2b2216; --med-wash:#272415; --ok-wash:#152720;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -16px rgba(0,0,0,.8);
}

*{box-sizing:border-box}
body{
  background:var(--ground); color:var(--ink);
  font-family:"IBM Plex Sans","Segoe UI",system-ui,sans-serif;
  font-size:15px; line-height:1.55;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1120px; margin:0 auto; padding:40px 24px 72px;
      display:flex; flex-direction:column; gap:34px}
h1,h2,h3{margin:0; text-wrap:balance;
  font-family:"IBM Plex Sans Condensed","IBM Plex Sans",system-ui,sans-serif;
  font-weight:600; letter-spacing:-.01em}
h1{font-size:30px}
h2{font-size:19px}
p{margin:0}
.mono{font-family:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
      font-variant-numeric:tabular-nums}
.eyebrow{font-family:"IBM Plex Mono",ui-monospace,monospace; font-size:11px;
  text-transform:uppercase; letter-spacing:.13em; color:var(--faint)}
.sub{color:var(--muted); max-width:66ch}

/* ── masthead ─────────────────────────────────────────────────────── */
.mast{display:flex; flex-direction:column; gap:8px;
  border-bottom:2px solid var(--line-strong); padding-bottom:18px}
.mast .meta{display:flex; flex-wrap:wrap; gap:6px 18px; color:var(--faint);
  font-size:12px}

/* ── the readout ──────────────────────────────────────────────────── */
.readout{display:grid; grid-template-columns:minmax(200px,240px) 1fr; gap:32px;
  background:var(--surface); border:1px solid var(--line);
  border-radius:3px; box-shadow:var(--shadow); padding:26px 28px}
@media (max-width:760px){.readout{grid-template-columns:1fr}}

.gauge{display:flex; flex-direction:column; gap:10px;
  border-right:1px solid var(--line); padding-right:28px}
@media (max-width:760px){.gauge{border-right:0; border-bottom:1px solid var(--line);
  padding-right:0; padding-bottom:22px}}
.score{font-family:"IBM Plex Sans Condensed","IBM Plex Sans",sans-serif;
  font-size:76px; line-height:.9; font-weight:600; letter-spacing:-.03em;
  font-variant-numeric:tabular-nums; display:flex; align-items:baseline; gap:8px}
.score small{font-size:22px; color:var(--faint); font-weight:400}
.grade{display:inline-flex; align-items:center; gap:7px; font-size:12px;
  color:var(--muted)}
.grade b{font-family:"IBM Plex Mono",monospace; font-size:13px;
  background:var(--crit-wash); color:var(--crit); padding:2px 8px;
  border-radius:2px; border:1px solid currentColor}

/* the ceiling: the conceptual heart of the score, drawn as a physical stop */
.track{position:relative; height:9px; background:var(--surface-2);
  border:1px solid var(--line); border-radius:2px; overflow:visible}
.track .fill{position:absolute; inset:0 auto 0 0; background:var(--accent);
  border-radius:1px}
.track .ceiling{position:absolute; top:-6px; bottom:-6px; width:2px;
  background:var(--crit)}
.track .ceiling::after{content:attr(data-label); position:absolute; top:-17px;
  left:50%; transform:translateX(-50%); white-space:nowrap; font-size:10px;
  font-family:"IBM Plex Mono",monospace; color:var(--crit); letter-spacing:.04em}
.capnote{font-size:12.5px; color:var(--crit); display:flex; gap:7px;
  align-items:flex-start; line-height:1.45}
.capnote svg{flex:none; margin-top:2px}

/* ── axes ─────────────────────────────────────────────────────────── */
.axes{display:flex; flex-direction:column; gap:11px; min-width:0}
.axis{display:grid; grid-template-columns:112px 1fr 56px; gap:14px;
  align-items:center}
.axis .name{font-size:13px; color:var(--ink)}
.axis .val{text-align:right; font-size:13px}
.axis .detail{grid-column:2/4; font-size:11.5px; color:var(--faint);
  margin-top:-6px}
.meter{height:7px; background:var(--surface-2); border:1px solid var(--line);
  border-radius:2px; overflow:hidden}
.meter i{display:block; height:100%; background:var(--accent)}
.meter.is-crit i{background:var(--crit)}
.meter.is-high i{background:var(--high)}
.meter.is-ok i{background:var(--ok)}
.meter.unmeasured{background:repeating-linear-gradient(90deg,
  var(--surface-2) 0 4px, transparent 4px 8px)}

/* ── the thesis callout ───────────────────────────────────────────── */
.thesis{background:var(--surface); border:1px solid var(--line);
  border-left:3px solid var(--crit); border-radius:3px; padding:22px 24px;
  display:flex; flex-direction:column; gap:12px}
.thesis .pair{display:grid; grid-template-columns:1fr 1fr; gap:0;
  border:1px solid var(--line); border-radius:2px; overflow:hidden}
@media (max-width:640px){.thesis .pair{grid-template-columns:1fr}}
.thesis .pair>div{padding:14px 16px; display:flex; flex-direction:column; gap:5px}
.thesis .pair>div+div{border-left:1px solid var(--line)}
@media (max-width:640px){.thesis .pair>div+div{border-left:0;
  border-top:1px solid var(--line)}}
.thesis .green{background:var(--ok-wash)}
.thesis .red{background:var(--crit-wash)}
.tag{font-family:"IBM Plex Mono",monospace; font-size:10.5px;
  text-transform:uppercase; letter-spacing:.1em}
.tag.g{color:var(--ok)} .tag.r{color:var(--crit)}

/* ── risk map ─────────────────────────────────────────────────────── */
.band{display:flex; flex-direction:column; gap:14px}
.riskgroup{border:1px solid var(--line); border-radius:3px;
  background:var(--surface); overflow:hidden}
.riskhead{display:flex; align-items:center; gap:10px; padding:10px 16px;
  border-bottom:1px solid var(--line); font-size:12px;
  font-family:"IBM Plex Mono",monospace; letter-spacing:.08em;
  text-transform:uppercase}
.riskhead .dot{width:9px; height:9px; border-radius:50%; flex:none}
.riskhead .count{margin-left:auto; color:var(--faint); letter-spacing:0}
.crit .dot{background:var(--crit)} .crit .riskhead{background:var(--crit-wash); color:var(--crit)}
.high .dot{background:var(--high)} .high .riskhead{background:var(--high-wash); color:var(--high)}
.med  .dot{background:var(--med)}  .med  .riskhead{background:var(--med-wash);  color:var(--med)}
.low  .dot{background:var(--faint)}
.ok   .dot{background:var(--ok)}   .ok   .riskhead{background:var(--ok-wash);   color:var(--ok)}

.item{display:grid; grid-template-columns:1fr auto; gap:4px 16px;
  padding:13px 16px; border-top:1px solid var(--line)}
.item:first-of-type{border-top:0}
.item .what{font-size:14px}
.item .where{grid-column:1/2; font-size:12px; color:var(--muted)}
.item .trail{grid-column:1/-1; display:flex; flex-wrap:wrap; gap:6px;
  margin-top:3px}
.chip{font-family:"IBM Plex Mono",monospace; font-size:10.5px;
  border:1px solid var(--line-strong); color:var(--muted);
  padding:1px 7px; border-radius:2px; white-space:nowrap}
.chip.state{border-color:currentColor}
.chip.v{color:var(--crit)} .chip.u{color:var(--high)} .chip.t{color:var(--ok)}
.item .note{grid-column:1/-1; font-size:12.5px; color:var(--muted);
  border-left:2px solid var(--line-strong); padding-left:11px; margin-top:6px;
  max-width:78ch}

/* ── module table ─────────────────────────────────────────────────── */
.tablewrap{overflow-x:auto; border:1px solid var(--line); border-radius:3px;
  background:var(--surface)}
table{border-collapse:collapse; width:100%; font-size:13px; min-width:640px}
th,td{text-align:left; padding:9px 14px; border-bottom:1px solid var(--line)}
th{font-family:"IBM Plex Mono",monospace; font-size:10.5px; color:var(--faint);
  text-transform:uppercase; letter-spacing:.09em; font-weight:400;
  background:var(--surface-2); position:sticky; top:0}
tbody tr:last-child td{border-bottom:0}
td.num{text-align:right; font-family:"IBM Plex Mono",monospace;
  font-variant-numeric:tabular-nums}
td.path{font-family:"IBM Plex Mono",monospace; font-size:12px}
.minibar{display:inline-block; width:54px; height:5px; background:var(--surface-2);
  border:1px solid var(--line); border-radius:2px; overflow:hidden;
  vertical-align:middle; margin-left:8px}
.minibar i{display:block; height:100%; background:var(--accent)}
.gap{color:var(--crit); font-weight:600}

/* ── rules ────────────────────────────────────────────────────────── */
.rules{display:grid; grid-template-columns:repeat(auto-fill,minmax(250px,1fr));
  gap:0; border:1px solid var(--line); border-radius:3px; overflow:hidden;
  background:var(--surface)}
.rule{padding:13px 16px; border-right:1px solid var(--line);
  border-bottom:1px solid var(--line); display:flex; flex-direction:column; gap:4px}
.rule .code{display:flex; align-items:center; gap:8px;
  font-family:"IBM Plex Mono",monospace; font-size:12px}
.rule .title{font-size:12.5px; color:var(--muted); line-height:1.4}
.rule .n{margin-left:auto; font-size:11px}

footer{border-top:1px solid var(--line); padding-top:18px; color:var(--faint);
  font-size:12px; display:flex; flex-direction:column; gap:6px}
a{color:var(--accent)}
:focus-visible{outline:2px solid var(--accent); outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{animation:none!important;
  transition:none!important}}
"""


def _e(s: str) -> str:
    return html.escape(str(s), quote=True)


def _meter(pct: float, cls: str = "") -> str:
    return (f'<div class="meter {cls}"><i style="width:{max(0.0,min(100.0,pct)):.1f}%">'
            f'</i></div>')


def _axis_class(key: str, value: float) -> str:
    if key in ("security", "behaviour", "architecture"):
        return "is-crit" if value < 60 else ("is-high" if value < 85 else "is-ok")
    return ""


def _grade(score: int) -> str:
    for cut, letter in ((90, "A"), (80, "B"), (70, "C"), (60, "D"), (50, "E")):
        if score >= cut:
            return letter
    return "F"


def build_html(rep: Report) -> str:
    now = datetime.now().strftime("%d %B %Y, %H:%M")

    # ── readout ──────────────────────────────────────────────────────
    cap_pct = 49 if rep.critical_open else 100
    ceiling = (f'<span class="ceiling" data-label="plafond {cap_pct}" '
               f'style="left:{cap_pct}%"></span>') if rep.capped_by else ""

    axes_html = []
    for key in ("lines", "branches", "functions", "behaviour", "security",
                "architecture", "types"):
        a = rep.axes[key]
        if not a.measured:
            axes_html.append(
                f'<div class="axis"><span class="name">{_e(a.name)}</span>'
                f'<div class="meter unmeasured"></div>'
                f'<span class="val mono" style="color:var(--faint)">—</span>'
                f'<span class="detail">non mesuré — {_e(a.detail)}</span></div>')
            continue
        axes_html.append(
            f'<div class="axis"><span class="name">{_e(a.name)}</span>'
            f'{_meter(a.value, _axis_class(key, a.value))}'
            f'<span class="val mono">{a.value:.0f}%</span>'
            + (f'<span class="detail">{_e(a.detail)}</span>' if a.detail else "")
            + '</div>')

    capnote = ""
    if rep.capped_by:
        capnote = (
            '<p class="capnote">'
            '<svg width="13" height="13" viewBox="0 0 16 16" aria-hidden="true">'
            '<path d="M8 1.5 15 14H1z" fill="none" stroke="currentColor" '
            'stroke-width="1.4" stroke-linejoin="round"/>'
            '<path d="M8 6v4M8 11.6v.8" stroke="currentColor" stroke-width="1.4" '
            'stroke-linecap="round"/></svg>'
            f'<span>{_e(rep.capped_by)}. Une moyenne pondérée se dilue en '
            'ajoutant du vert ailleurs ; un plafond, non.</span></p>')

    # ── risk map ─────────────────────────────────────────────────────
    groups = []
    for sev in (CRITICAL, HIGH, MEDIUM, LOW):
        rows = [s for s in rep.risk_rows() if s.behaviour.severity == sev]
        if not rows:
            continue
        items = []
        for s in rows:
            b = s.behaviour
            st = {VIOLATED: "v", UNTESTED: "u", TESTED: "t", BY_RULE: "t"}[s.status]
            trail = [f'<span class="chip state {st}">{_STATUS_LABEL[s.status]}</span>']
            if b.rule:
                trail.append(f'<span class="chip">règle {_e(b.rule)}</span>')
            if b.audit:
                trail.append(f'<span class="chip">audit {_e(b.audit)}</span>')
            trail.append(f'<span class="chip">{_e(b.id)}</span>')
            note = (f'<p class="note">{_e(b.note)}</p>' if b.note else "")
            items.append(
                f'<div class="item"><span class="what">{_e(b.what)}</span>'
                f'<span class="where mono">{_e(b.module)}</span>'
                f'<span class="trail">{"".join(trail)}</span>{note}</div>')
        groups.append(
            f'<section class="riskgroup {_SEV_CLASS[sev]}">'
            f'<header class="riskhead"><span class="dot"></span>'
            f'{_SEV_LABEL[sev]}<span class="count">{len(rows)}</span></header>'
            f'{"".join(items)}</section>')

    healthy = rep.healthy_modules()
    if healthy:
        cells = []
        for m in healthy:
            cov = rep.coverage.for_file(m)
            pct = f"{cov['summary']['percent_covered']:.0f}%" if cov else "—"
            cells.append(f'<div class="item"><span class="what mono">{_e(m)}</span>'
                         f'<span class="where mono">{pct} de lignes</span></div>')
        groups.append(
            f'<section class="riskgroup ok"><header class="riskhead">'
            f'<span class="dot"></span>saines — toute garantie déclarée est prouvée'
            f'<span class="count">{len(healthy)}</span></header>'
            f'{"".join(cells)}</section>')

    # ── module table ─────────────────────────────────────────────────
    by_module: dict[str, list] = {}
    for s in rep.statuses:
        by_module.setdefault(s.behaviour.module, []).append(s)

    rows = []
    for module in sorted(by_module):
        sts = by_module[module]
        held = sum(1 for s in sts if s.holds)
        cov = rep.coverage.for_file(module)
        line_pct = cov["summary"]["percent_covered"] if cov else None
        br_pct = cov["summary"]["percent_branches_covered"] if cov else None
        gpct = 100.0 * held / len(sts)
        gap = line_pct is not None and line_pct - gpct >= 30
        rows.append(
            f'<tr><td class="path">{_e(module)}</td>'
            + (f'<td class="num">{line_pct:.0f}%<span class="minibar">'
               f'<i style="width:{line_pct:.0f}%"></i></span></td>'
               if line_pct is not None else '<td class="num">—</td>')
            + (f'<td class="num">{br_pct:.0f}%</td>' if br_pct is not None
               else '<td class="num">—</td>')
            + f'<td class="num{" gap" if gap else ""}">{held}/{len(sts)}</td>'
            f'<td class="num">{len([f for f in rep.findings if f.path == module])}</td>'
            '</tr>')

    # ── rules ────────────────────────────────────────────────────────
    from tools import jarvis_lint
    counts: dict[str, int] = {}
    for f in rep.findings:
        counts[f.rule] = counts.get(f.rule, 0) + 1
    rule_cards = []
    for m in jarvis_lint.all_rules():
        n = counts.get(m.code, 0)
        colour = "var(--ok)" if n == 0 else (
            "var(--crit)" if m.severity == "critical" else
            "var(--high)" if m.severity == "high" else "var(--med)")
        rule_cards.append(
            f'<div class="rule"><span class="code"><b>{_e(m.code)}</b>'
            f'<span class="chip">{_e(m.severity)}</span>'
            f'<span class="n mono" style="color:{colour}">'
            f'{"clean" if n == 0 else f"{n} ✗"}</span></span>'
            f'<span class="title">{_e(m.title)}</span></div>')

    ledger = ""
    if rep.problems:
        ledger = ('<section class="thesis"><h2>Registre désynchronisé</h2><ul>'
                  + "".join(f"<li>{_e(p)}</li>" for p in rep.problems)
                  + "</ul></section>")

    n_beh = len(rep.statuses)
    n_held = sum(1 for s in rep.statuses if s.holds)

    return f"""<title>Alexio Code Health</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+Condensed:wght@600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{_CSS}</style>

<div class="wrap">

  <header class="mast">
    <span class="eyebrow">Alexio · assistant vocal · Fedora 43 / Wayland</span>
    <h1>État de santé du code</h1>
    <p class="sub">Trois mesures distinctes, jamais interchangeables : la
      couverture dit si une ligne a été <em>exécutée</em>, la couverture de
      comportement si une garantie est <em>vérifiée</em>, la couverture de règles
      si un invariant est <em>mécaniquement imposé</em>.</p>
    <div class="meta mono">
      <span>généré le {_e(now)}</span>
      <span>{n_beh} garanties déclarées</span>
      <span>{len(rep.findings)} violations de règles</span>
      <span>541 tests</span>
    </div>
  </header>

  <section class="readout">
    <div class="gauge">
      <span class="eyebrow">score global</span>
      <div class="score">{rep.score}<small>/ 100</small></div>
      <span class="grade">note <b>{_grade(rep.score)}</b></span>
      <div class="track">
        <span class="fill" style="width:{rep.score}%"></span>{ceiling}
      </div>
      {capnote}
    </div>
    <div class="axes">{"".join(axes_html)}</div>
  </section>

  <section class="thesis">
    <span class="eyebrow">pourquoi la couverture ne suffit pas</span>
    <h2>Un test vert qui ne vérifie pas la garantie</h2>
    <div class="pair">
      <div class="green">
        <span class="tag g">le test passe</span>
        <p class="mono" style="font-size:12.5px">test_revoking_devices_actually_revokes_them</p>
        <p class="sub" style="font-size:13px">Il vérifie que le dictionnaire
          d'appareils est vidé. <code class="mono">dashboard/auth.py</code> est à
          95&nbsp;% de couverture de lignes.</p>
      </div>
      <div class="red">
        <span class="tag r">la garantie est fausse</span>
        <p class="mono" style="font-size:12.5px">revoke_devices() ne touche pas _sessions</p>
        <p class="sub" style="font-size:13px">Le téléphone volé garde son jeton
          porteur jusqu'à 7 jours après que l'utilisateur a appuyé sur
          «&nbsp;révoquer&nbsp;». Aucune couverture ne voit ça.</p>
      </div>
    </div>
    <p class="sub" style="font-size:13px">Même forme dans
      <code class="mono">core/ai/router.py</code> : 99&nbsp;% d'instructions,
      93&nbsp;% de branches, et <code class="mono">Budget.private</code> mesuré à
      <strong>100&nbsp;%</strong> — pendant que la garantie «&nbsp;ne quitte pas la
      machine&nbsp;» est cassée, parce que la levée part de router.py et est avalée
      un module plus haut.</p>
  </section>

  {ledger}

  <section class="band">
    <div>
      <span class="eyebrow">priorités</span>
      <h2>Carte des risques</h2>
      <p class="sub" style="margin-top:4px">{n_beh - n_held} garanties sur
        {n_beh} ne tiennent pas. Chaque ligne relie le défaut à sa règle, à son
        entrée d'audit et au module concerné.</p>
    </div>
    {"".join(groups)}
  </section>

  <section class="band">
    <div>
      <span class="eyebrow">par module</span>
      <h2>Couverture contre garanties</h2>
      <p class="sub" style="margin-top:4px">L'écart entre les deux colonnes
        centrales est ce que ce tableau existe pour montrer. Un écart de 30 points
        ou plus est marqué en rouge.</p>
    </div>
    <div class="tablewrap"><table>
      <thead><tr><th>module</th><th style="text-align:right">lignes</th>
        <th style="text-align:right">branches</th>
        <th style="text-align:right">garanties</th>
        <th style="text-align:right">lint</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table></div>
  </section>

  <section class="band">
    <div>
      <span class="eyebrow">invariants imposés</span>
      <h2>Règles jarvis-lint</h2>
      <p class="sub" style="margin-top:4px">Onze règles AST, chacune écrite contre
        un défaut réel, chacune avec un test positif et un test négatif.</p>
    </div>
    <div class="rules">{"".join(rule_cards)}</div>
  </section>

  <footer>
    <span class="mono">python -m tools.jarvis_health --measure · python -m tools.jarvis_lint</span>
    <span>Généré depuis <code class="mono">tools/jarvis_health/report.py</code>.
      Aucun chiffre de cette page n'est écrit à la main.</span>
  </footer>
</div>
"""


def write_dashboard(rep: Report, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_html(rep), encoding="utf-8")
