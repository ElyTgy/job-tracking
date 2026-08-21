"""Stats + charts over the tracked companies and their open internship postings.

    python -m scraper.analyze            # writes data/analysis.html + prints a summary

Sectors are hand-assigned (there is no industry column in the DB); role families
and countries are keyword-derived from title / location.
"""
import html
import json
import re
from collections import Counter, defaultdict

from .db import connect, ROOT

# --------------------------------------------------------------------------- sectors
SECTORS = {
 "Robotics & humanoids": "1X,Alloy Robotics,Anvil Robotics,Apptronik,Avestec,BitRobot,Bonsai Robotics,Boston Dynamics,Bracket Bot,Cyberworks Robotics,D1 Humanoid,Dyna Robotics,Fauna Robotics,Figure,Flash Forest,Flexion,Flyby Robotics,General Robotics,Generalist AI,Genesis AI,Holiday Robotics,Humanoid,HumonOS,K-Scale Labs,Kraken Robotics,Matic Robots,Mecka,Physical Intelligence,Planar Motor,Proception,Promise Robotics,Red Rabbit Robotics,Sanctuary AI,Skild AI,Standard Bots,Sunday Robotics,Unitree Robotics,WUJI TECH,smartARM,GelSight",
 "AI chips & semiconductors": "AMD,Applied Brain Research,Blumind,Cerebras,Cognichip,Etched,Exa Laboratories,Extropic,Fab2,Fractal Semiconductor,Groq,Inversion Semiconductor,MatX,Microchip,Micron,NVIDIA,Nordic Semiconductor,Normal Computing,OmniVision,Qualcomm,Rain AI,Rebellions,Ricursive Intelligence,SemiVision,Sonera,StarIC,Stathera,Substrate,Synapse Semi,TSMC,Taalas,Tenstorrent,The Six Semiconductor,Unconventional AI,Untether AI,d-Matrix,u-blox,Diode Computers,Quilter",
 "AI labs & AI software": "AMI Labs,Alpyne,Ambition Labs,Anthropic,Arena Physica,Assemble Labs,Axiomatic_AI,Boardy,CuspAI,Dynamics Lab,Elai,Eon,FLORA,FirstPrinciples,Google DeepMind,Gumloop,Hugging Face,HyperTunnel,Interlatent,Kara,Kyber Labs,METR,Maneva,Mechanize,Midjourney,Mistral,Modular,Niantic Spatial,Omen.ai,OnDeck Fisheries AI,OneCup AI,OpenAI,Prime Intellect,Radical AI,Reducto,Saiwa,Shiraz AI,Standard Intelligence,Thinking Machines Lab,Veeda AI,Zeromatter,alphaXiv,tnkr,Qoherent,Everstar,Dominant Information Solutions Canada",
 "Neurotech & BCI": "Aleph Neuro,Attune Neurosciences,Axo Neurotech,Axoft,Coherence Neuro,Cortical Labs,E11 Bio,Efference,Forest Neurotech,INBRAIN Neuroelectronics,Integral Neurotech,Merge Labs,Motif Neurotech,Neuralink,Neurobionics,Nudge,ONWARD Medical,Paradromics,Precision Neuroscience,Salvia Bioelectronics,Science Corporation,Subsense,Synchron,The Biological Compute Co.",
 "Space & satellites": "Aethero,Astranis,Canada Rocket Company,Foundation Space Resources,GRU Space,Impulse Space,Kepler Communications,LUNR Aerospace,MDA Space,Magnestar,Mission Control,NASA,NordSpace,Northwood,PSC by Rocket Lab,Pacific Rim Space Exploration Corp,Planet Labs,Reflect Orbital,SpaceX,StarSpec Technologies,Varda Space Industries,Vast Space,Volta Space Technologies,Wyvern,Xiphos,Xona Space Systems",
 "Aerospace, drones & defense": "Anduril,Archer Aviation,Astro Mechanica,Boom Supersonic,Dominion Dynamics,Kon Aerosystems,Reliable Robotics,Skydio,VertoAeris Systems,Bubble Technology Industries,Canadian Strategic Missions Corporation,Valthos,Pilgrim Labs",
 "Fusion, nuclear & energy": "Aalo Atomics,Antares,Apollo Atomics,Base Power Company,Commonwealth Fusion Systems,Corvus Energy,General Fusion,Helion Energy,Interlock Energy,Marathon Fusion,Maritime Fusion,Oklo,Pacific Fusion,Pulsar Fusion,Radiant,Terraform Industries,Valar Atomics,Applied Electrodynamics",
 "Biotech & medtech": "28Bio,Arc Institute,Asimov,BenchSci,CZ Biohub,Cortisonic,Epiloid Biotech,Ginkgo Bioworks,Kardium,Medra,Perennial Diagnostics,Precigenetics,Steadiwear,Synaptive Medical,Tenomix,Until,Vital Bio",
 "Quantum": "D-Wave,IonQ,Photonic,PsiQuantum,Quantum Circuits,Quetzal,SBQuantum",
 "Photonics & optics": "Ayar Labs,Dream Photonics,Lightmatter,Mesh Optical,Mojo Vision,Phantom Photonics",
 "Autonomous vehicles": "Applied Intuition,Nuro,Tesla,Waabi,comma.ai",
 "Hardware, electronics & devices": "Atomic Industries,Cortex Design,ForceN,HELIX SENSORS,Keirton,MesoMat,MistyWest,Opal,Opal Electronics,Orbital Research,Oura,Oxide Computer,Tomorrow Lab,Turing Pi,Verdi,pamir.ai,t0.technology,Siemens,TRIUMF",
 "Big tech & software": "Amazon,Databricks,Figma,Google,Microsoft,Obsidian,Internet Backyard,Intract,hey girlie,heyclicky",
 "Fintech & trading": "Connor Clark & Lunn Investment Management,LayerZero,Optiver,Ramp",
}
COMPANY_SECTOR = {}
for sector, names in SECTORS.items():
    for n in names.split(","):
        COMPANY_SECTOR[n.strip().lower()] = sector

# --------------------------------------------------------------------------- roles
# (family, regex) — first match wins, so order is priority.
STRIP = re.compile(r"amazon university talent acquisition|university recruiting|undergrad student science recruiting|talent pool", re.I)
ROLE_RULES = [
 ("Data & analytics", r"data analyst|business intelligence|analytics"),
 ("Business, ops & finance", r"finance|financ|account|tax|marketing|sales|market|legal|\bhr\b|hris|human resources|talent|recruit|program manag|product manag|\bpm\b|pmt|business|growth|strategy|supply chain|purchasing|sourcing|operations|\bops\b|area manager|loss prevention|public policy|executive assistant|instock|brand specialist|demand generation|customer success|customer solutions|solution engineer|solutions architect|cloud solution|trader|trading|consulting|member experience|communications|events|social media|planning ie|\bie\b|project intern|partnerships|shop tech|librarian|logistics|facilit|technician|specialist|quantitative|quant\b|\bgtm\b|commercial|vendor management|maintenance|\bgrc\b|legal"),
 ("Chip design (RTL / DV / PD / analog)", r"\brtl\b|\bdv\b|design verification|physical design|\bpd\b|\bdft\b|si/pi|\bic design|digital design|analog|mixed.signal|vlsi|asic|silicon|dram design|dram ip|circuits|\bcpu\b|chiplet|\bhbm\b|formal verification|computer architecture|digital ip|chip simulation|performance verification|\bsoc\b|system-on-chip"),
 ("Semiconductor process & fab", r"process|etch|litho|\bcmp\b|wafer|\bfab\b|photomask|metals|thin film|device eng|cell reliab|nand|contamination|yield|\btem\b|amhs|probe|failure analysis|module test|qem|\bpie\b|\bpi\b"),
 ("Machine learning / AI research", r"machine learning|\bml\b|\bai\b|artificial intelligence|deep learning|applied scien|research|computer vision|reinforcement|diffusion|foundation model|embodied|inference|genai|generative|algorithm|perception|intelligence|data scien|neuroengineer|llm|agent|distillation|mlff"),
 ("Firmware & embedded", r"firmware|embedded|flight software|\bdsp\b"),
 ("Test, QA & reliability", r"\btest\b|testing|\bqa\b|quality|reliability|validation|sqa|swqa|product assurance|assembly, integration|product development"),
 ("Robotics, controls & mechatronics", r"robot|controls|mechatronic|autonom|dynamics|motion|navigation"),
 ("Software engineering", r"software|\bswe\b|\bsw\b|developer|full stack|fullstack|backend|frontend|infrastructure|compiler|\bweb\b|devops|\bit\b|security|systems admin|automation|tools|supercomputing|performance|data|digital|enterprise|applications|sde|design engineer"),
 ("Electrical & hardware eng", r"electrical|electronics|hardware|pcb|power electronics|avionics|fpga|\brf\b|radiation|optical|optics|photon|wireless|modem|wifi|signal"),
 ("Mechanical & manufacturing", r"mechanical|\bmech\b|manufactur|cnc|thermal|composites|\bcad\b|industrial|naval|civil|materials|machine park|supplier quality|product design"),
 ("Bio & lab science", r"bio|cell|in vivo|preclinical|microfab|electrochem|laboratory|lab\b|neuro|chem"),
 ("General / unspecified engineering", r"engineer|systems|system architecture|nuclear|aero|technical|r&d|architecture"),
 ("General / unspecified", r"."),
]
ROLE_RES = [(f, re.compile(rx, re.I)) for f, rx in ROLE_RULES]


def role_family(title: str) -> str:
    t = STRIP.sub("", title).lower()
    for fam, rx in ROLE_RES:
        if rx.search(t):
            return fam
    return "General / unspecified"


# --------------------------------------------------------------------------- geography
US_STATES = {
 "AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California","CO":"Colorado","CT":"Connecticut",
 "DE":"Delaware","FL":"Florida","GA":"Georgia","HI":"Hawaii","ID":"Idaho","IL":"Illinois","IN":"Indiana","IA":"Iowa",
 "KS":"Kansas","KY":"Kentucky","LA":"Louisiana","ME":"Maine","MD":"Maryland","MA":"Massachusetts","MI":"Michigan",
 "MN":"Minnesota","MS":"Mississippi","MO":"Missouri","MT":"Montana","NE":"Nebraska","NV":"Nevada","NH":"New Hampshire",
 "NJ":"New Jersey","NM":"New Mexico","NY":"New York","NC":"North Carolina","ND":"North Dakota","OH":"Ohio","OK":"Oklahoma",
 "OR":"Oregon","PA":"Pennsylvania","RI":"Rhode Island","SC":"South Carolina","SD":"South Dakota","TN":"Tennessee",
 "TX":"Texas","UT":"Utah","VT":"Vermont","VA":"Virginia","WA":"Washington","WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming",
 "DC":"District of Columbia",
}
STATE_NAMES = {v.lower(): v for v in US_STATES.values()}
US_CITIES = {  # city -> state, for locations that only name a city
 "san francisco":"CA","sf":"CA","south san francisco":"CA","sunnyvale":"CA","santa clara":"CA","san jose":"CA","san carlos":"CA",
 "mountain view":"CA","palo alto":"CA","redwood city":"CA","emeryville":"CA","hawthorne":"CA","san mateo":"CA","hayward":"CA",
 "milpitas":"CA","long beach":"CA","folsom":"CA","irvine":"CA","costa mesa":"CA","los angeles":"CA","berkeley":"CA",
 "menlo park":"CA","el segundo":"CA","san diego":"CA","rialto":"CA",
 "austin":"TX","dallas":"TX","fort worth":"TX","bastrop":"TX","brownsville":"TX","mcgregor":"TX",
 "seattle":"WA","redmond":"WA","bellevue":"WA","chicago":"IL","matteson":"IL","boise":"ID","idaho falls":"ID",
 "boston":"MA","cambridge":"MA","north reading":"MA","boxborough":"MA","centennial":"CO","longmont":"CO","fort collins":"CO",
 "broomfield":"CO","colorado springs":"CO","new york":"NY","nyc":"NY","rochester":"NY","fishkill":"NY","secaucus":"NJ","somerset":"NJ",
 "atlanta":"GA","phoenix":"AZ","akron":"OH","arlington":"VA","reston":"VA","fredericksburg":"VA","manassas":"VA",
 "westboro":"WI","cape canaveral":"FL","tucson":"AZ",
}
CANADA_MARKERS = ["canada", ", can", "ontario", "toronto", "vancouver", "montreal", "montréal", "waterloo", "ottawa",
                  "brampton", "sainte-anne", "québec", "quebec", "british columbia", "hamilton, on", "burnaby", "markham",
                  "whitby", "calgary", "edmonton", "kitchener"]
CANADA_PROV_RE = re.compile(r",\s*(ON|BC|QC|AB|MB|SK|NS|NB)\b")

# Companies whose postings carry no location field: where the internship actually is.
HQ_FALLBACK = {
 "axoft": ("US", "MA"), "aalo atomics": ("US", "TX"), "nasa": ("US", None), "tsmc": ("US", "AZ"),
 "comma.ai": ("US", "CA"), "spacex": ("US", None),
 "helix sensors": ("CA", None), "lunr aerospace": ("CA", None), "mission control": ("CA", None),
 "orbital research": ("CA", None), "verdi": ("CA", None), "mesomat": ("CA", None),
 "cortical labs": ("OTHER", None), "omnivision": ("OTHER", None), "rebellions": ("OTHER", None),
}


def geo(company: str, title: str, location: str):
    """Return (countries:set[str] of 'US'|'CA'|'OTHER'|'UNKNOWN', us_states:set[str])."""
    loc = (location or "").strip()
    text = loc if loc else ""
    # titles sometimes carry the city when the location field is blank
    if not loc:
        m = re.search(r"[-–]\s*([A-Za-z .]+?),\s*([A-Z]{2})\b", title)
        if m:
            text = f"{m.group(1)}, {m.group(2)}"
        elif re.search(r"\bcambridge\b", title, re.I) and company.lower() == "axoft":
            text = "Cambridge, MA"
    if not text:
        fb = HQ_FALLBACK.get(company.lower())
        if fb:
            c, st = fb
            return {c}, ({st} if st else set())
        return {"UNKNOWN"}, set()

    countries, states = set(), set()
    low = text.lower()
    if loc.lower().startswith("flexible - any spacex"):
        return {"US"}, set()

    if any(mk in low for mk in CANADA_MARKERS) or CANADA_PROV_RE.search(text):
        # guard: ", CA," / ", CA" is California when it follows a US city
        countries.add("CA")

    us_hit = bool(re.search(r"united states|\busa\b|u\.s\.", low))
    for abbr in re.findall(r"\b([A-Z]{2})\b", text):
        if abbr in US_STATES and abbr != "CA":
            states.add(abbr); us_hit = True
    # California abbreviation: only when paired with a city we know or "United States"
    if re.search(r",\s*CA\b", text) and ("canada" not in low or "santa clara" in low or "california" in low):
        states.add("CA"); us_hit = True
    for name, canon in STATE_NAMES.items():
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            states.add({v: k for k, v in US_STATES.items()}[canon]); us_hit = True
    for city, st in US_CITIES.items():
        if re.search(r"(^|[\s,;(])" + re.escape(city) + r"($|[\s,;)])", low):
            states.add(st); us_hit = True
    if "remote" in low and not states and not countries:
        countries.add("REMOTE")
    if us_hit:
        countries.add("US")
    if "CA" in countries and countries == {"CA"} and "santa clara" in low:
        countries = {"US", "CA"}
    if not countries:
        countries.add("OTHER")
    return countries, states


# --------------------------------------------------------------------------- analysis
def load():
    conn = connect()
    comps = [dict(r) for r in conn.execute("SELECT * FROM companies ORDER BY name")]
    posts = [dict(r) for r in conn.execute(
        "SELECT p.*, c.name AS company FROM postings p JOIN companies c ON c.id=p.company_id WHERE p.closed=0 ORDER BY c.name, p.title")]
    return comps, posts


def analyze():
    comps, posts = load()
    for c in comps:
        c["sector"] = COMPANY_SECTOR.get(c["name"].lower(), "Other / unclassified")
    sector_of = {c["name"]: c["sector"] for c in comps}
    for p in posts:
        p["sector"] = sector_of[p["company"]]
        p["family"] = role_family(p["title"])
        p["countries"], p["states"] = geo(p["company"], p["title"], p["location"] or "")

    out = {}
    out["n_companies"] = len(comps)
    out["n_active"] = sum(1 for c in comps if c["discovery_status"] == "ok")
    out["n_posts"] = len(posts)
    out["companies_with_posts"] = len({p["company"] for p in posts})
    out["sector_companies"] = Counter(c["sector"] for c in comps)
    out["sector_posts"] = Counter(p["sector"] for p in posts)
    out["sector_companies_with_posts"] = Counter(sector_of[n] for n in {p["company"] for p in posts})
    out["family_all"] = Counter(p["family"] for p in posts)
    out["country"] = Counter()
    for p in posts:
        for c in p["countries"]:
            out["country"][c] += 1
    out["top_companies"] = Counter(p["company"] for p in posts)
    out["tag"] = Counter(p["tag"] for p in posts)

    def subset(code):
        return [p for p in posts if code in p["countries"]]
    out["ca_posts"] = subset("CA")
    out["us_posts"] = subset("US")
    out["ca_family"] = Counter(p["family"] for p in out["ca_posts"])
    out["us_family"] = Counter(p["family"] for p in out["us_posts"])
    out["ca_sector"] = Counter(p["sector"] for p in out["ca_posts"])
    out["us_sector"] = Counter(p["sector"] for p in out["us_posts"])
    out["ca_companies"] = Counter(p["company"] for p in out["ca_posts"])
    out["us_companies"] = Counter(p["company"] for p in out["us_posts"])
    out["us_states"] = Counter()
    for p in out["us_posts"]:
        for s in p["states"]:
            out["us_states"][s] += 1
        if not p["states"]:
            out["us_states"]["(multi-site / unspecified)"] += 1
    # state x family matrix for the US
    out["us_cities"] = Counter()
    for p in out["us_posts"]:
        low = (p["location"] or p["title"]).lower()
        hits = {c for c in US_CITIES if re.search(r"(^|[\s,;(])" + re.escape(c) + r"($|[\s,;)])", low)}
        hits -= {"sf"} if "san francisco" in hits else set()
        for c in hits:
            out["us_cities"][f"{c.title() if c != 'sf' else 'San Francisco'}, {US_CITIES[c]}"] += 1
        if not hits:
            out["us_cities"]["(multi-site / city not listed)"] += 1
    out["us_state_family"] = defaultdict(Counter)
    for p in out["us_posts"]:
        for s in (p["states"] or {"(multi-site / unspecified)"}):
            out["us_state_family"][s][p["family"]] += 1
    out["posts"] = posts
    out["comps"] = comps
    return out


# --------------------------------------------------------------------------- HTML report
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
PALETTE_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]


def esc(s):
    return html.escape(str(s))


def hbar(counter, title, subtitle="", top=None, total=None, seq=True, color_idx=0):
    """Single-series horizontal bar chart as inline SVG (sequential/one hue)."""
    items = counter.most_common(top) if top else counter.most_common()
    if not items:
        return ""
    total = total or sum(counter.values())
    mx = max(v for _, v in items)
    row_h, label_w, bar_max, pad = 26, 250, 420, 8
    h = row_h * len(items) + 30
    w = label_w + bar_max + 90
    parts = [f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(title)}">']
    for i, (k, v) in enumerate(items):
        y = 20 + i * row_h
        bw = max(2, round(bar_max * v / mx))
        pct = 100 * v / total
        parts.append(
            f'<g class="row"><title>{esc(k)}: {v} ({pct:.0f}%)</title>'
            f'<text class="lbl" x="{label_w - pad}" y="{y + 16}" text-anchor="end">{esc(k)}</text>'
            f'<rect class="bar s{color_idx}" x="{label_w}" y="{y + 3}" width="{bw}" height="18" rx="4" ry="4"/>'
            f'<rect class="bar s{color_idx}" x="{label_w}" y="{y + 3}" width="{min(bw, 4)}" height="18"/>'
            f'<text class="val" x="{label_w + bw + pad}" y="{y + 16}">{v} <tspan class="muted">({pct:.0f}%)</tspan></text></g>')
    parts.append("</svg>")
    cap = f'<figure><figcaption><strong>{esc(title)}</strong>{(" — " + esc(subtitle)) if subtitle else ""}</figcaption>{"".join(parts)}</figure>'
    return cap


def stacked(matrix, families, title, subtitle=""):
    """Stacked horizontal bars: rows = matrix keys, segments = families (fixed order, max 8 + Other)."""
    fams = families[:7]
    rows = sorted(matrix.items(), key=lambda kv: -sum(kv[1].values()))
    row_h, label_w, bar_max, pad = 26, 230, 430, 8
    mx = max(sum(c.values()) for _, c in rows)
    h = row_h * len(rows) + 30
    w = label_w + bar_max + 70
    parts = [f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="{esc(title)}">']
    for i, (k, c) in enumerate(rows):
        y = 20 + i * row_h
        x = label_w
        tot = sum(c.values())
        parts.append(f'<text class="lbl" x="{label_w - pad}" y="{y + 16}" text-anchor="end">{esc(k)}</text>')
        segs = [(f, c.get(f, 0)) for f in fams] + [("Other", sum(v for f, v in c.items() if f not in fams))]
        for j, (f, v) in enumerate(segs):
            if v <= 0:
                continue
            sw = max(1, round(bar_max * v / mx))
            cls = f"s{j}" if j < 7 else "s-other"
            parts.append(f'<rect class="bar {cls}" x="{x}" y="{y + 3}" width="{max(sw - 2, 1)}" height="18"><title>{esc(k)} · {esc(f)}: {v}</title></rect>')
            x += sw
        parts.append(f'<text class="val" x="{x + pad}" y="{y + 16}">{tot}</text>')
    parts.append("</svg>")
    legend = "".join(f'<span class="key"><i class="sw s{j}"></i>{esc(f)}</span>' for j, f in enumerate(fams)) + '<span class="key"><i class="sw s-other"></i>Other</span>'
    return f'<figure><figcaption><strong>{esc(title)}</strong>{(" — " + esc(subtitle)) if subtitle else ""}</figcaption><div class="legend">{legend}</div>{"".join(parts)}</figure>'


def table(rows, headers):
    th = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="tbl"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def posting_table(posts, with_state=False):
    rows = []
    for p in sorted(posts, key=lambda p: (p["company"], p["title"])):
        r = [p["company"], p["title"], p["family"], p["location"] or "(not listed)"]
        if with_state:
            r.append(", ".join(sorted(p["states"])) or "multi-site")
        rows.append(r)
    hdr = ["Company", "Role", "Family", "Location"] + (["State"] if with_state else [])
    return table(rows, hdr)


CSS = """
:root{--bg:#fcfcfb;--card:#ffffff;--ink:#0b0b0b;--ink2:#52514e;--muted:#8a8984;--line:#e6e5e0;
 --s0:#2a78d6;--s1:#eb6834;--s2:#1baf7a;--s3:#eda100;--s4:#e87ba4;--s5:#008300;--s6:#4a3aa7;--s7:#e34948;--other:#b8b7b1}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#1a1a19;--card:#222221;--ink:#fff;--ink2:#c3c2b7;--muted:#8f8e88;--line:#33332f;
 --s0:#3987e5;--s1:#d95926;--s2:#199e70;--s3:#c98500;--s4:#d55181;--s5:#008300;--s6:#9085e9;--s7:#e66767;--other:#5c5b56}}
:root[data-theme="dark"]{--bg:#1a1a19;--card:#222221;--ink:#fff;--ink2:#c3c2b7;--muted:#8f8e88;--line:#33332f;
 --s0:#3987e5;--s1:#d95926;--s2:#199e70;--s3:#c98500;--s4:#d55181;--s5:#008300;--s6:#9085e9;--s7:#e66767;--other:#5c5b56}
body{background:var(--bg);color:var(--ink);font:15px/1.5 "IBM Plex Sans",system-ui,sans-serif;font-variant-numeric:tabular-nums;margin:0;padding:32px 20px 80px}
main{max-width:960px;margin:0 auto}
h1{font-size:30px;margin:0 0 4px}h2{font-size:22px;margin:48px 0 8px;padding-top:16px;border-top:1px solid var(--line)}h3{font-size:16px;margin:28px 0 6px}
p.sub{color:var(--ink2);margin:0 0 20px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tile .l{color:var(--ink2);font-size:13px}.tile .v{font-size:28px;font-weight:600;font-family:"IBM Plex Mono","IBM Plex Sans",monospace}
figure{margin:16px 0;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;overflow-x:auto}
figcaption{margin-bottom:8px}figcaption strong{font-weight:600}
svg.chart{width:100%;max-width:760px;height:auto;display:block}
.lbl{fill:var(--ink2);font-size:12px}.val{fill:var(--ink);font-size:12px}.muted{fill:var(--muted)}
.bar.s0{fill:var(--s0)}.bar.s1{fill:var(--s1)}.bar.s2{fill:var(--s2)}.bar.s3{fill:var(--s3)}.bar.s4{fill:var(--s4)}.bar.s5{fill:var(--s5)}.bar.s6{fill:var(--s6)}.bar.s7{fill:var(--s7)}.bar.s-other{fill:var(--other)}
.row:hover .bar{opacity:.8}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:var(--ink2);margin:4px 0 8px}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:middle}
.sw.s0{background:var(--s0)}.sw.s1{background:var(--s1)}.sw.s2{background:var(--s2)}.sw.s3{background:var(--s3)}.sw.s4{background:var(--s4)}.sw.s5{background:var(--s5)}.sw.s6{background:var(--s6)}.sw.s-other{background:var(--other)}
.tbl{overflow-x:auto;border:1px solid var(--line);border-radius:10px;margin:12px 0}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--ink2);font-weight:600;background:var(--card);position:sticky;top:0}
details summary{cursor:pointer;color:var(--ink2);margin:8px 0}
.note{color:var(--ink2);font-size:13px}
"""


def render(a):
    fam_order = [f for f, _ in a["family_all"].most_common()]
    co = a["country"]
    tiles = [
        ("Companies tracked", a["n_companies"]), ("Scrapeable (ATS found)", a["n_active"]),
        ("Open internship postings", a["n_posts"]), ("Companies with ≥1 posting", a["companies_with_posts"]),
        ("Postings in Canada", co.get("CA", 0)), ("Postings in the US", co.get("US", 0)),
        ("Elsewhere (non-US/CA)", co.get("OTHER", 0)), ("Location unknown", co.get("UNKNOWN", 0)),
    ]
    tiles_html = "".join(f'<div class="tile"><div class="l">{esc(l)}</div><div class="v">{v}</div></div>' for l, v in tiles)

    sector_rows = []
    for s, n in a["sector_companies"].most_common():
        sector_rows.append([s, n, a["sector_companies_with_posts"].get(s, 0), a["sector_posts"].get(s, 0),
                            a["ca_sector"].get(s, 0), a["us_sector"].get(s, 0)])
    sector_tbl = table(sector_rows, ["Sector", "Companies", "…with open internships", "Open postings", "in Canada", "in US"])

    # sector -> family matrix (overall)
    sec_fam = defaultdict(Counter)
    for p in a["posts"]:
        sec_fam[p["sector"]][p["family"]] += 1

    comp_sector_list = defaultdict(list)
    for c in a["comps"]:
        comp_sector_list[c["sector"]].append(c["name"])
    sector_lists = "".join(f"<details><summary>{esc(s)} ({len(v)})</summary><p class='note'>{esc(', '.join(sorted(v)))}</p></details>"
                           for s, v in sorted(comp_sector_list.items(), key=lambda kv: -len(kv[1])))

    us_state_rows = [[US_STATES.get(s, s), n] for s, n in a["us_states"].most_common()]
    ca_city = Counter()
    for p in a["ca_posts"]:
        loc = (p["location"] or p["title"]).lower()
        city = next((c for c in ["toronto", "vancouver", "waterloo", "montreal", "montréal", "sainte-anne", "brampton", "quebec", "québec", "hamilton", "whitby", "ottawa"] if c in loc), "unspecified")
        ca_city[{"sainte-anne": "Sainte-Anne-de-Bellevue (Montréal)", "québec": "Quebec City", "quebec": "Quebec City", "montréal": "Montréal"}.get(city, city.title())] += 1

    h = [f'<title>Internship Landscape</title><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;600&family=IBM+Plex+Mono:wght@500&display=swap"><style>{CSS}</style><main>',
         "<h1>Internship landscape</h1>",
         f"<p class='sub'>{a['n_companies']} companies you follow · {a['n_posts']} open internship / co-op postings as of the last scrape. "
         "Sectors are hand-assigned; role families and countries are derived from posting titles and locations.</p>",
         f"<div class='tiles'>{tiles_html}</div>",
         "<h2>1 · What areas do these companies work in?</h2>",
         hbar(a["sector_companies"], "Companies by sector", f"all {a['n_companies']} tracked companies"),
         hbar(a["sector_posts"], "Open internship postings by sector", "every posting, all countries", color_idx=1),
         "<p class='note'>Postings skew hard toward a handful of large ATS boards (Amazon, Micron, Qualcomm, AMD, Optiver, Tenstorrent) — most of their postings are outside North America. The Canada/US sections below are the useful view.</p>",
         sector_tbl,
         "<details><summary>Which companies are in each sector</summary>" + sector_lists + "</details>",
         "<h2>2 · What internship roles are offered? (overall)</h2>",
         hbar(a["family_all"], "Postings by role family", "all countries", color_idx=2),
         stacked(sec_fam, fam_order, "Role families within each sector", "all countries; top 7 families + Other"),
         hbar(a["top_companies"], "Companies with the most open postings", "top 20", top=20, color_idx=3),
         "<h2>3 · Canada</h2>",
         f"<p class='sub'>{len(a['ca_posts'])} postings across {len(a['ca_companies'])} companies.</p>",
         hbar(a["ca_family"], "Canadian postings by role family", color_idx=2),
         hbar(a["ca_sector"], "Canadian postings by sector", color_idx=1),
         hbar(ca_city, "Canadian postings by city", color_idx=0),
         hbar(a["ca_companies"], "Canadian postings by company", color_idx=3),
         "<h3>All Canadian postings</h3>", posting_table(a["ca_posts"]),
         "<h2>4 · United States</h2>",
         f"<p class='sub'>{len(a['us_posts'])} postings across {len(a['us_companies'])} companies. Multi-site postings count once per state they list.</p>",
         hbar(a["us_family"], "US postings by role family", color_idx=2),
         hbar(a["us_sector"], "US postings by sector", color_idx=1),
         hbar(Counter({US_STATES.get(s, s): n for s, n in a["us_states"].items()}), "US postings by state", "a posting listing several states counts in each", color_idx=0),
         hbar(a["us_cities"], "US postings by city", "top 25", top=25, color_idx=0),
         stacked({US_STATES.get(s, s): c for s, c in a["us_state_family"].items()}, fam_order, "Role families by US state", "top 7 families + Other"),
         hbar(a["us_companies"], "US postings by company", top=25, color_idx=3),
         "<h3>All US postings</h3>", posting_table(a["us_posts"], with_state=True),
         "<h2>Method notes</h2>",
         "<ul class='note'><li>Only <em>open</em> postings (closed=0) are counted. Pinned standing postings (e.g. 'Internships / Co-op' pages) count as one.</li>"
         "<li>A posting listing several locations counts in every country/state it lists, so Canada + US + elsewhere can exceed the total.</li>"
         "<li>Blank locations fall back to the city in the title, then to the company's known site (Axoft→MA, Aalo→TX, TSMC→AZ, SpaceX→US, HELIX/LUNR/Mission Control/Orbital Research/Verdi/MesoMat→Canada).</li>"
         "<li>Role family = first matching keyword rule on the title; 'Business, ops & finance' also absorbs quant/trading and non-engineering roles.</li></ul>",
         "</main>"]
    return "\n".join(h)


def main():
    a = analyze()
    out = ROOT / "data" / "analysis.html"
    out.write_text(render(a))
    # console summary
    print(f"companies={a['n_companies']} postings={a['n_posts']} countries={dict(a['country'])}")
    print("\nSECTORS (companies / postings):")
    for s, n in a["sector_companies"].most_common():
        print(f"  {s:34s} {n:4d} / {a['sector_posts'].get(s,0)}")
    print("\nROLE FAMILIES overall / CA / US:")
    for f, n in a["family_all"].most_common():
        print(f"  {f:40s} {n:4d} / {a['ca_family'].get(f,0):3d} / {a['us_family'].get(f,0):3d}")
    print("\nUS STATES:")
    for s, n in a["us_states"].most_common():
        print(f"  {US_STATES.get(s,s):30s} {n}")
    print("\nCA companies:", dict(a["ca_companies"]))
    unk = [(p['company'], p['title'], p['location']) for p in a['posts'] if 'UNKNOWN' in p['countries']]
    print("\nUNKNOWN location:", len(unk)); [print("  ", u) for u in unk]
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
