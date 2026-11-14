"""Shared responsive navigation, design tokens, and accessible theme controls."""
import re


NAV_ITEMS = (
    ("/", "Dashboard"),
    ("/sarathi", "Sarathi"),
    ("/#upload", "My Resume"),
    ("/candidate-profile", "Candidate Profile"),
    ("/careers", "Career Preferences"),
    ("/find-jobs", "Find Jobs"),
    ("/workflow", "Job Search Workflow"),
    ("/applications", "Applications"),
    ("/onboarding", "Onboarding"),
    ("/application-settings", "Settings"),
    ("/employers", "Employer Sources"),
)


def apply_ui_theme(document: str) -> str:
    """Add one consistent, responsive app shell without changing page behavior."""
    if not isinstance(document, str):
        return document
    if 'id="bsa-theme-style"' in document and 'id="bsa-theme-controller"' in document:
        return document

    # Two legacy pages emit fragments without explicit head/body elements.
    # Normalize those documents so the shared shell is valid and predictable.
    if not re.search(r"<body\b", document, re.I):
        html_open = re.search(r"<html\b[^>]*>", document, re.I)
        if html_open:
            content = re.search(r"<(?:main|header|h1|form|section)\b", document[html_open.end():], re.I)
            if content:
                point = html_open.end() + content.start()
                if not re.search(r"<head\b", document[:point], re.I):
                    document = document[:html_open.end()] + "<head>" + document[html_open.end():]
                    point += len("<head>")
                document = document[:point] + "</head><body>" + document[point:]
                if re.search(r"</html\s*>", document, re.I):
                    document = re.sub(r"</html\s*>", "</body></html>", document, count=1, flags=re.I)

    style = r"""<style id="bsa-theme-style">
:root{color-scheme:light;--bsa-ink:#17263b;--bsa-muted:#586a80;--bsa-line:#dce4ee;--bsa-surface:#fff;--bsa-wash:#f5f7fa;--bsa-link:#2457a6;--bsa-brand:#2859a5;--bsa-focus:#2867c7;--bsa-success:#176b48;--bsa-error:#a12c32;--bsa-warning:#805315}
html[data-bsa-theme="dark"]{color-scheme:dark;--bsa-ink:#e7edf6;--bsa-muted:#b2bfd0;--bsa-line:#35445a;--bsa-surface:#182337;--bsa-wash:#101827;--bsa-link:#a9c6ff;--bsa-brand:#a9c6ff;--bsa-focus:#a9c6ff;--bsa-success:#92dfb5;--bsa-error:#ffaaa5;--bsa-warning:#f2cc83}
*{box-sizing:border-box}html{min-width:320px;scroll-padding-top:76px}body{width:auto!important;max-width:none!important;min-height:100vh;margin:0!important;padding:0 0 0 258px!important;background:var(--bsa-wash)!important;color:var(--bsa-ink)!important;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif!important;line-height:1.55}
body,body *{box-sizing:border-box}body>h1,body>p,body>main,body>header,body>section,body>.wrap,body>.page,body>.container{max-width:1100px}body>:not(.bsa-sidebar):not(.bsa-mobile-bar):not(.bsa-mobile-shade):not(script):not(style){width:min(calc(100% - 48px),1100px)!important;margin-left:auto!important;margin-right:auto!important}
.bsa-sidebar{position:fixed;z-index:1000;inset:0 auto 0 0;width:258px;padding:24px 16px 18px;background:var(--bsa-surface);border-right:1px solid var(--bsa-line);display:flex;flex-direction:column;gap:18px;overflow-y:auto}.bsa-brand{display:flex;align-items:center;gap:10px;padding:4px 10px 18px;color:var(--bsa-ink);text-decoration:none;font-size:18px;font-weight:750;letter-spacing:-.03em}.bsa-brand-mark{display:grid;place-items:center;width:34px;height:34px;border-radius:11px;background:#eaf1fc;color:#204d91;font-size:17px}.bsa-sidebar nav{display:grid!important;gap:4px!important;align-content:start!important}.bsa-sidebar nav a{display:flex;align-items:center;min-height:42px;padding:9px 11px;border-radius:9px;color:var(--bsa-muted)!important;text-decoration:none;font-size:14px;font-weight:570;line-height:1.3}.bsa-sidebar nav a:hover{background:var(--bsa-wash);color:var(--bsa-ink)!important}.bsa-sidebar nav a[aria-current="page"]{background:#edf3fc;color:#214f91!important;font-weight:700}.bsa-side-footer{margin-top:auto;padding:12px 10px 0;border-top:1px solid var(--bsa-line);color:var(--bsa-muted);font-size:12px}.bsa-theme-toggle{min-height:38px;padding:7px 10px;border:1px solid var(--bsa-line);border-radius:8px;background:var(--bsa-surface);color:var(--bsa-ink);font:600 13px/1.2 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;cursor:pointer}.bsa-mobile-bar{display:none}
html[data-bsa-theme="dark"] body{background:var(--bsa-wash)!important;color:var(--bsa-ink)!important}html[data-bsa-theme="dark"] header,html[data-bsa-theme="dark"] section,html[data-bsa-theme="dark"] main,html[data-bsa-theme="dark"] .top,html[data-bsa-theme="dark"] .panel,html[data-bsa-theme="dark"] .section,html[data-bsa-theme="dark"] .toolbar,html[data-bsa-theme="dark"] .app,html[data-bsa-theme="dark"] .job,html[data-bsa-theme="dark"] .metric,html[data-bsa-theme="dark"] .card,html[data-bsa-theme="dark"] .notice,html[data-bsa-theme="dark"] .step,html[data-bsa-theme="dark"] .tile,html[data-bsa-theme="dark"] article{background:var(--bsa-surface)!important;color:var(--bsa-ink)!important;border-color:var(--bsa-line)!important}
html[data-bsa-theme="dark"] input,html[data-bsa-theme="dark"] textarea,html[data-bsa-theme="dark"] select{background:#111b2b!important;color:var(--bsa-ink)!important;border-color:#52627a!important}html[data-bsa-theme="dark"] input::placeholder,html[data-bsa-theme="dark"] textarea::placeholder{color:#aab8ca!important;opacity:1}html[data-bsa-theme="dark"] a{color:var(--bsa-link)}html[data-bsa-theme="dark"] .muted,html[data-bsa-theme="dark"] .sub,html[data-bsa-theme="dark"] .source,html[data-bsa-theme="dark"] p{color:var(--bsa-muted)}html[data-bsa-theme="dark"] h1,html[data-bsa-theme="dark"] h2,html[data-bsa-theme="dark"] h3,html[data-bsa-theme="dark"] h4,html[data-bsa-theme="dark"] label{color:var(--bsa-ink)}html[data-bsa-theme="dark"] .diagnostics{background:#202d42!important;color:var(--bsa-ink)!important;border-color:var(--bsa-line)!important}html[data-bsa-theme="dark"] .error{color:#ffaaa5!important}html[data-bsa-theme="dark"] hr,html[data-bsa-theme="dark"] .job{border-color:var(--bsa-line)!important}html[data-bsa-theme="dark"] button:not(.primary):not([class*="primary"]){background:var(--bsa-surface);color:var(--bsa-ink);border-color:var(--bsa-line)}
button,input,textarea,select{font-family:inherit}button{cursor:pointer}input,textarea,select{max-width:100%;color:var(--bsa-ink)}body input:not([type="checkbox"]):not([type="radio"]),body textarea,body select{padding:10px 12px;border:1px solid var(--bsa-line);border-radius:8px;background:var(--bsa-surface);color:var(--bsa-ink);font:inherit}body input[type="checkbox"],body input[type="radio"]{accent-color:var(--bsa-brand)}body input[type="file"]{padding:8px}body textarea{min-height:112px;resize:vertical}body a:not(.bsa-brand):not(.bsa-mobile-brand){color:var(--bsa-link);text-decoration:none;font-weight:550}body a:not(.bsa-brand):not(.bsa-mobile-brand):hover{text-decoration:underline}body button:not(.bsa-theme-toggle):not(.bsa-menu-toggle){min-height:40px;padding:9px 14px;border:1px solid var(--bsa-line);border-radius:8px;background:var(--bsa-surface);color:var(--bsa-ink);font:600 14px/1.3 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}body button:hover:not(:disabled):not(.bsa-theme-toggle):not(.bsa-menu-toggle){border-color:var(--bsa-brand)}body p,body small,body li,body code,body pre,body .muted,body .source,body .diagnostic{overflow-wrap:anywhere}body pre{white-space:pre-wrap}:focus-visible{outline:3px solid var(--bsa-focus)!important;outline-offset:2px!important}a{overflow-wrap:anywhere}button:disabled{cursor:not-allowed}[role="status"]{overflow-wrap:anywhere}body>h1{padding-top:22px;font-size:clamp(28px,4vw,38px);letter-spacing:-.035em}body>.panel,body>main,body>section,body>header,body>article{border-radius:14px}body>.panel{background:var(--bsa-surface);border:1px solid var(--bsa-line);padding:22px}body button.primary,body button[class*="primary"]{background:var(--bsa-brand);border-color:var(--bsa-brand);color:#fff}html[data-bsa-theme="dark"] body button.primary,html[data-bsa-theme="dark"] body button[class*="primary"]{color:#111b2b}html[data-bsa-theme="dark"] body>.panel{background:var(--bsa-surface)!important;border-color:var(--bsa-line)!important}
html[data-bsa-theme="dark"] .bsa-sidebar{background:#131e2e;border-color:var(--bsa-line)}html[data-bsa-theme="dark"] .bsa-sidebar nav a[aria-current="page"]{background:#293a55;color:#edf2f9!important}html[data-bsa-theme="dark"] .bsa-brand-mark{background:#293a55;color:#edf2f9}
@media(max-width:760px){body{padding:68px 0 0!important}.bsa-sidebar{inset:58px auto 0 0;width:min(300px,86vw);transform:translateX(-105%);transition:transform .18s ease;box-shadow:0 14px 30px #0c1b2b22}.bsa-sidebar[data-open="true"]{transform:translateX(0)}.bsa-sidebar nav{gap:2px!important}.bsa-mobile-bar{position:fixed;z-index:1001;inset:0 0 auto;height:58px;padding:0 14px;background:var(--bsa-surface);border-bottom:1px solid var(--bsa-line);display:flex;align-items:center;justify-content:space-between}.bsa-mobile-brand{font-weight:750;color:var(--bsa-ink);text-decoration:none}.bsa-mobile-controls{display:flex;gap:8px;align-items:center}.bsa-menu-toggle{min-height:38px;padding:6px 10px;background:var(--bsa-surface);color:var(--bsa-ink);border:1px solid var(--bsa-line);border-radius:8px;font:600 13px/1.2 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.bsa-side-footer{display:flex;align-items:center;justify-content:space-between}.bsa-mobile-shade{position:fixed;z-index:999;inset:58px 0 0;background:#0a142466}.bsa-mobile-shade[hidden]{display:none!important}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition:none!important;animation:none!important}}
</style>"""
    if "</head>" in document:
        document = document.replace("</head>", style + "</head>", 1)
    else:
        document = document.replace("<body", style + "<body", 1)

    launcher_style = r"""<style id="bsa-sarathi-launcher-style">
#bsa-sarathi-launcher{position:fixed!important;z-index:1100;right:22px;bottom:20px;width:auto!important;margin:0!important;border:0!important;border-radius:999px!important;padding:12px 17px!important;background:var(--bsa-brand)!important;color:#fff!important;box-shadow:0 8px 26px #10233a33;font-weight:700!important}
#bsa-sarathi-panel{position:fixed;z-index:1099;right:20px;bottom:78px;width:min(430px,calc(100vw - 32px));height:min(680px,calc(100dvh - 108px));border:1px solid var(--bsa-line);border-radius:16px;background:var(--bsa-surface);box-shadow:0 18px 48px #10233a38;overflow:hidden}
#bsa-sarathi-panel[hidden]{display:none!important}#bsa-sarathi-panel header{height:48px!important;width:100%!important;margin:0!important;padding:7px 12px!important;display:flex;align-items:center;justify-content:space-between;background:var(--bsa-surface)!important;border-bottom:1px solid var(--bsa-line)}#bsa-sarathi-panel header button{min-height:32px!important;padding:4px 9px!important}#bsa-sarathi-frame{display:block;width:100%;height:calc(100% - 48px);border:0;background:var(--bsa-surface)}
@media(max-width:760px){#bsa-sarathi-launcher{right:12px;bottom:12px}#bsa-sarathi-panel{inset:58px 0 0;width:100vw;height:calc(100dvh - 58px);border-radius:0;border-left:0;border-right:0;border-bottom:0}html[data-bsa-theme="dark"] #bsa-sarathi-launcher{color:#111b2b!important}}
</style>"""
    if "</head>" in document and 'id="bsa-sarathi-launcher-style"' not in document:
        document = document.replace("</head>", launcher_style + "</head>", 1)

    path_match = re.search(r"<body\b[^>]*>", document, re.I)
    if not path_match:
        return document
    # Replace an existing page nav, when present, instead of leaving two competing navs.
    existing_nav = re.compile(r"<nav\b[^>]*>.*?</nav>", re.I | re.S)
    document = existing_nav.sub("", document, count=1)
    links = []
    for href, label in NAV_ITEMS:
        current = ' aria-current="page"' if href.split("#", 1)[0] == "/" and href == "/" else ""
        links.append(f'<a href="{href}"{current}>{label}</a>')
    sidebar = ('<aside class="bsa-sidebar" id="bsa-sidebar" aria-label="Application navigation">'
               '<a class="bsa-brand" href="/"><span class="bsa-brand-mark" aria-hidden="true">B</span><span>BabyStepsAI</span></a>'
               '<nav aria-label="Main navigation">' + "".join(links) + '</nav>'
               '<div class="bsa-side-footer"><span>Private workspace</span><button class="bsa-theme-toggle" type="button">Dark mode</button></div></aside>')
    mobile = ('<header class="bsa-mobile-bar"><a class="bsa-mobile-brand" href="/">BabyStepsAI</a>'
              '<div class="bsa-mobile-controls"><button class="bsa-theme-toggle" type="button">Dark mode</button>'
              '<button class="bsa-menu-toggle" type="button" aria-controls="bsa-sidebar" aria-expanded="false">Menu</button></div></header>'
              '<div class="bsa-mobile-shade" hidden></div>')
    insert_at = path_match.end()
    document = document[:insert_at] + sidebar + mobile + document[insert_at:]
    launcher = ('<section id="bsa-sarathi-panel" aria-label="Sarathi chat panel" hidden>'
                '<header><strong>Sarathi · Your guide</strong><button type="button" id="bsa-sarathi-close" aria-label="Close Sarathi chat">Close</button></header>'
                '<iframe id="bsa-sarathi-frame" title="Sarathi conversational assistant" src="about:blank" loading="lazy"></iframe></section>'
                '<button id="bsa-sarathi-launcher" type="button" aria-controls="bsa-sarathi-panel" aria-expanded="false">Ask Sarathi</button>')
    document = document.replace("</body>", launcher + "</body>", 1)
    controller = r"""<script id="bsa-theme-controller">(()=>{const key='babysteps-theme',root=document.documentElement;let mode='light';try{mode=localStorage.getItem(key)||'light'}catch{}root.dataset.bsaTheme=mode;const toggles=[...document.querySelectorAll('.bsa-theme-toggle')],sidebar=document.getElementById('bsa-sidebar'),menu=document.querySelector('.bsa-menu-toggle'),shade=document.querySelector('.bsa-mobile-shade'),syncActive=()=>{const path=location.pathname;for(const a of document.querySelectorAll('.bsa-sidebar nav a')){const link=new URL(a.href,location.href),active=link.pathname===path&&(link.pathname!=='/'||(link.hash?link.hash===location.hash:!location.hash));if(active)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current')}};syncActive();addEventListener('hashchange',syncActive);const paint=()=>{const dark=root.dataset.bsaTheme==='dark';for(const b of toggles){b.textContent=dark?'Light mode':'Dark mode';b.setAttribute('aria-pressed',String(dark))}};const syncMobile=()=>{const mobile=matchMedia('(max-width:760px)').matches,open=sidebar?.getAttribute('data-open')==='true';if(sidebar){sidebar.inert=mobile&&!open;sidebar.setAttribute('aria-hidden',String(mobile&&!open))}if(menu)menu.hidden=!mobile;if(!mobile&&shade)shade.hidden=true};const close=()=>{const restore=sidebar?.contains(document.activeElement),mobile=matchMedia('(max-width:760px)').matches;sidebar?.setAttribute('data-open','false');menu?.setAttribute('aria-expanded','false');if(shade)shade.hidden=true;syncMobile();if(restore&&mobile)menu?.focus()};for(const b of toggles)b.addEventListener('click',()=>{root.dataset.bsaTheme=root.dataset.bsaTheme==='dark'?'light':'dark';try{localStorage.setItem(key,root.dataset.bsaTheme)}catch{}paint()});menu?.addEventListener('click',()=>{const open=sidebar?.getAttribute('data-open')!=='true';sidebar?.setAttribute('data-open',String(open));menu.setAttribute('aria-expanded',String(open));if(shade)shade.hidden=!open;syncMobile();if(open)sidebar?.querySelector('a')?.focus()});shade?.addEventListener('click',close);document.addEventListener('keydown',e=>{if(e.key==='Escape')close()});sidebar?.querySelectorAll('a').forEach(a=>a.addEventListener('click',close));addEventListener('resize',syncMobile);syncMobile();paint()})();</script>"""
    document = document.replace("</body>", controller + "</body>", 1)
    launcher_controller = r"""<script id="bsa-sarathi-launcher-controller">(()=>{const button=document.getElementById('bsa-sarathi-launcher'),panel=document.getElementById('bsa-sarathi-panel'),frame=document.getElementById('bsa-sarathi-frame'),close=document.getElementById('bsa-sarathi-close');if(!button||!panel||!frame)return;let loaded=false;const sendTheme=()=>{try{frame.contentWindow?.postMessage({type:'bsa-theme',theme:document.documentElement.dataset.bsaTheme==='dark'?'dark':'light'},location.origin)}catch{}};const observer=new MutationObserver(sendTheme);observer.observe(document.documentElement,{attributes:true,attributeFilter:['data-bsa-theme']});frame.addEventListener('load',sendTheme);const hide=()=>{panel.hidden=true;button.setAttribute('aria-expanded','false');button.focus()};button.addEventListener('click',()=>{panel.hidden=!panel.hidden;const open=!panel.hidden;button.setAttribute('aria-expanded',String(open));if(open){if(!loaded){frame.src='/sarathi?compact=1';loaded=true}close?.focus();sendTheme()}});close?.addEventListener('click',hide);document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!panel.hidden)hide()})})();</script>"""
    return document.replace("</body>", launcher_controller + "</body>", 1)
