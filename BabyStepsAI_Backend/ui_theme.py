"""Small shared, accessible theme/navigation layer for server-rendered pages."""
import re


def apply_ui_theme(document: str) -> str:
    if not isinstance(document, str) or "data-bsa-theme" in document:
        return document
    style = r"""<style id="bsa-theme-style">
:root{color-scheme:light;--bsa-ink:#17263b;--bsa-muted:#66758a;--bsa-line:#dfe7f1;--bsa-surface:#fff;--bsa-wash:#f4f7fb;--bsa-link:#2859a5}
html[data-bsa-theme="dark"]{color-scheme:dark;--bsa-ink:#e7edf6;--bsa-muted:#a8b5c8;--bsa-line:#334156;--bsa-surface:#182337;--bsa-wash:#101827;--bsa-link:#9ec0ff}
html[data-bsa-theme="dark"] body{background:var(--bsa-wash)!important;color:var(--bsa-ink)!important}
html[data-bsa-theme="dark"] header,html[data-bsa-theme="dark"] .top,html[data-bsa-theme="dark"] .panel,html[data-bsa-theme="dark"] .section,html[data-bsa-theme="dark"] .toolbar,html[data-bsa-theme="dark"] .app,html[data-bsa-theme="dark"] .job,html[data-bsa-theme="dark"] .metric,html[data-bsa-theme="dark"] .card,html[data-bsa-theme="dark"] .notice,html[data-bsa-theme="dark"] .step,html[data-bsa-theme="dark"] .tile{background:var(--bsa-surface)!important;color:var(--bsa-ink)!important;border-color:var(--bsa-line)!important}
html[data-bsa-theme="dark"] input,html[data-bsa-theme="dark"] textarea,html[data-bsa-theme="dark"] select{background:#111b2b!important;color:var(--bsa-ink)!important;border-color:#46556c!important}
html[data-bsa-theme="dark"] a{color:var(--bsa-link)!important}html[data-bsa-theme="dark"] .muted,html[data-bsa-theme="dark"] .sub,html[data-bsa-theme="dark"] .source{color:var(--bsa-muted)!important}
.bsa-global-nav{display:flex;flex-wrap:wrap;gap:14px;align-items:center;padding:10px max(18px,calc((100% - 1120px)/2));background:var(--bsa-surface);border-bottom:1px solid var(--bsa-line);font:600 14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.bsa-global-nav a{color:var(--bsa-link);text-decoration:none}.bsa-theme-toggle{margin-left:auto;font:inherit;border:1px solid var(--bsa-line);border-radius:8px;padding:7px 10px;background:var(--bsa-surface);color:var(--bsa-ink);cursor:pointer}.bsa-theme-toggle:focus-visible,.bsa-global-nav a:focus-visible{outline:3px solid #4b8dff;outline-offset:2px}
@media(max-width:680px){.bsa-global-nav{gap:10px;font-size:13px;padding:10px 14px}}
</style>"""
    document = document.replace("</head>", style + "</head>", 1)
    links = ("<nav class=\"bsa-global-nav\" aria-label=\"Main navigation\">"
             "<a href=\"/\">Dashboard</a><a href=\"/#upload\">My Resume</a>"
             "<a href=\"/candidate-profile\">Candidate Profile</a><a href=\"/careers\">Career Preferences</a>"
             "<a href=\"/find-jobs\">Find Jobs</a><a href=\"/workflow\">Job Search Workflow</a><a href=\"/applications\">Applications</a>"
             "<a href=\"/application-settings\">Settings</a><button class=\"bsa-theme-toggle\" type=\"button\">Dark mode</button></nav>")
    existing_nav = re.compile(r"<nav\b[^>]*>.*?</nav>", re.I | re.S)
    if existing_nav.search(document):
        document = existing_nav.sub(lambda _match: links, document, count=1)
    else:
        document, count = re.subn(r"(<body\b[^>]*>)", r"\1" + links, document, count=1, flags=re.I)
        if not count:
            return document
    script = r"""<script id="bsa-theme-controller">(()=>{const key='babysteps-theme';let mode='light';try{mode=localStorage.getItem(key)||'light'}catch{}const root=document.documentElement;root.dataset.bsaTheme=mode;const button=document.querySelector('.bsa-theme-toggle');const paint=()=>{if(button){const dark=root.dataset.bsaTheme==='dark';button.textContent=dark?'Light mode':'Dark mode';button.setAttribute('aria-pressed',String(dark))}};paint();button?.addEventListener('click',()=>{const next=root.dataset.bsaTheme==='dark'?'light':'dark';root.dataset.bsaTheme=next;try{localStorage.setItem(key,next)}catch{}paint()})})();</script>"""
    return document.replace("</body>", script + "</body>", 1)
