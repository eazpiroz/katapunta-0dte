"""Aplica el tema "petróleo + turquesa" de Katapunta a una página HTML.
Uso: python3 tools/tema.py ARCHIVO.html [--back] [--logo] [--fav]
  --back  agrega el botón "← Centro de mando" (subdominios)
  --fav   reemplaza el favicon por el símbolo turquesa
  --logo  reemplaza el logo horizontal embebido (portada)
Idempotente: si el tema ya está aplicado, lo reemplaza."""
import sys, re, base64, os
p = sys.argv[1]; flags = set(sys.argv[2:])
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ident = "/home/claude/katapunta-identidad/turquesa"
s = open(p).read()

FONTS = '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700;800&family=Lora:ital@1&family=IBM+Plex+Mono:wght@400;500;600&display=swap">'
CSS = """<style id="kp-tema">
/* Tema Katapunta · petróleo + turquesa (solo oscuro) */
:root,:root:not(#kp):not([data-theme="light"]),:root[data-theme]:not(#kp),:root:not(#kp){
  --bg:#0B1617; --surface:#112123; --sunk:#0D1B1D; --ink:#F2EFE8; --muted:#93A9A9; --line:#1E3739;
  --brand:#5CC1BF; --brand-ink:#5CC1BF; --lima:#8ADBD7; --navy:#5CC1BF; --navy-2:#4AA9A7;
  --gold:#5CC1BF; --gold-ink:#5CC1BF; --gold-soft:#123536;
  --good:#5FD4A0; --good-bg:#10302A; --warn:#E6B655; --warn-bg:#2E2711; --bad:#F07F74; --bad-bg:#351A18;
  --info:#7FC8E6; --info-bg:#11283A; --idle:#7E9393;
  --track-sl:#5A2A26; --track-tp:#1D4A3C; --grid:#183032; --chipbg:#5CC1BF; --chipfg:#0B1617; --lineC:#F2EFE8;
  --f-d:"Poppins",system-ui,sans-serif; --f-b:"Poppins",system-ui,sans-serif; --f-n:"IBM Plex Mono",ui-monospace,Menlo,monospace;
  --f-display:"Poppins",system-ui,sans-serif; --f-body:"Poppins",system-ui,sans-serif; --f-num:"IBM Plex Mono",ui-monospace,Menlo,monospace;
  color-scheme:dark;
}
html,body{background-color:#0B1617 !important;color:#F2EFE8}
body{background-image:linear-gradient(rgba(92,193,191,.045) 1px,transparent 1px),linear-gradient(90deg,rgba(92,193,191,.045) 1px,transparent 1px) !important;background-size:56px 56px !important;background-attachment:fixed}
h1{letter-spacing:-.02em}
.chip[aria-pressed="true"],.btn.primary,button.primary,.primary{color:#0B1617 !important}
.kp-back{display:inline-flex;align-items:center;gap:8px;align-self:flex-start;font:600 12px/1 "Poppins",system-ui,sans-serif;letter-spacing:.14em;text-transform:uppercase;color:#5CC1BF;text-decoration:none;border:1px solid #1E3739;border-radius:999px;padding:9px 14px;min-height:36px;background:rgba(17,33,35,.7);margin-bottom:6px}
.kp-back:hover{border-color:#5CC1BF;background:#112123}
.kp-back:focus-visible{outline:2px solid #5CC1BF;outline-offset:2px}
.kp-italic{font-family:"Lora",Georgia,serif;font-style:italic}
</style>"""
BACK = '<a class="kp-back" href="https://www.katapunta.com/" aria-label="Volver al Centro de mando de Katapunta">← Centro de mando</a>'

s = re.sub(r'<style id="kp-tema">.*?</style>', '', s, flags=re.S)
s = re.sub(r'<link rel="stylesheet" href="https://fonts.googleapis.com/css2\?family=Poppins[^>]*>', '', s)
s = s.replace(BACK, '')
# fuentes: reemplaza la hoja de Google Fonts existente
s, nf = re.subn(r'<link rel="stylesheet" href="https://fonts.googleapis.com/css2\?[^>]*>', FONTS, s, count=1)
if not nf:
    s = s.replace('</title>', '</title>\n' + FONTS, 1)
# tema: después del primer bloque <style> que no sea el nuestro (gana por orden y especificidad)
idx = s.find('</style>')
s = s[:idx + 8] + "\n" + CSS + s[idx + 8:]
if "--back" in flags:
    if '<div class="wrap">' in s:
        s = s.replace('<div class="wrap">', '<div class="wrap">\n  ' + BACK, 1)
    else:
        s = re.sub(r'(<body[^>]*>)', r'\1\n' + BACK, s, count=1)
b64 = lambda f: "data:image/png;base64," + base64.b64encode(open(f, "rb").read()).decode()
if "--fav" in flags:
    s = re.sub(r'<link rel="icon"[^>]*>', '<link rel="icon" type="image/png" href="' + b64(ident + "/favicon.png") + '">', s)
if "--logo" in flags:
    s = re.sub(r'(<img[^>]*class="[^"]*logo[^"]*"[^>]*src=")data:image/png;base64,[^"]+', r'\1' + b64(ident + "/horiz-web.png"), s)
    s = re.sub(r'(<img[^>]*src=")data:image/png;base64,[^"]+("[^>]*class="[^"]*logo)', r'\1' + b64(ident + "/horiz-web.png") + r'\2', s)
open(p, "w").write(s)
print("tema aplicado:", p, sorted(flags))
