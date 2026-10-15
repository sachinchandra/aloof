"""Customer email layout: one table-based HTML shell (single 560px column, Ember palette, system fonts, bulletproof button,
dark-mode aware) plus a matching plain-text body. No tracking pixels, no remote CSS. Logo is a PNG on www.aloof.si."""
import html as _h
from . import site

LOGO = f"{site.SITE}/email-logo.png"
LEGAL = "Aloof is operated by Enigmatic IT Solutions FZE, UAE."
INK, MUTED, LINE, PAGE, CARD, ACC, ACC_DK = "#1a120b", "#6b5a4b", "#eadfd2", "#f6f1ea", "#ffffff", "#ff7a1a", "#c2410c"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"
e = _h.escape

def para(t): return f'<p style="margin:0 0 16px;font-size:16px;line-height:1.6;color:{INK}" class="ink">{t}</p>'
def small(t): return f'<p style="margin:0 0 12px;font-size:14px;line-height:1.55;color:{MUTED}" class="muted">{t}</p>'
def code(t): return (f'<p style="margin:0 0 20px;padding:12px 14px;background:#fbf6ef;border:1px solid {LINE};border-radius:8px;font-family:{MONO};'
                     f'font-size:14px;line-height:1.5;color:{INK};word-break:break-all" class="code">{e(t)}</p>')
def button(label, url):
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 18px"><tr><td align="center" bgcolor="{ACC}" '
            f'style="border-radius:10px;background:{ACC}"><a href="{e(url, True)}" target="_blank" style="display:inline-block;padding:14px 28px;'
            f'font-family:{FONT};font-size:16px;font-weight:700;line-height:1.2;color:{INK};text-decoration:none;border-radius:10px;'
            f'background:{ACC}" class="btn">{e(label)}</a></td></tr></table>'
            f'<p style="margin:0 0 22px;font-size:13px;line-height:1.5;color:{MUTED};word-break:break-all" class="muted">Button not working? Paste this link into your browser:<br>'
            f'<a href="{e(url, True)}" style="color:{ACC_DK}" class="lnk">{e(url)}</a></p>')

def render(title, blocks, reason, preheader="", unsub=None):
    """blocks: list of HTML strings built with para/small/code/button. Returns the full HTML document."""
    foot = e(reason) + (f' <a href="{e(unsub, True)}" style="color:{ACC_DK}" class="lnk">Unsubscribe</a>.' if unsub else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark"><title>{e(title)}</title>
<style>
:root{{color-scheme:light dark;supported-color-schemes:light dark}}
@media (max-width:600px){{.wrap{{padding:20px 12px!important}}.pad{{padding:28px 22px!important}}.h1{{font-size:22px!important}}.btn{{display:block!important;text-align:center}}}}
@media (prefers-color-scheme:dark){{.bg{{background:#0f0b08!important}}.card{{background:#1a140f!important;border-color:#2e241b!important}}.ink,.h1{{color:#f6f1ec!important}}
.muted{{color:#b9ab9d!important}}.lnk{{color:#ffb020!important}}.code{{background:#241c15!important;border-color:#382d24!important;color:#f6f1ec!important}}.rule{{border-color:#2e241b!important}}}}
</style></head>
<body class="bg" style="margin:0;padding:0;background:{PAGE};font-family:{FONT};-webkit-text-size-adjust:100%">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:{PAGE}">{e(preheader)}&#8199;&#847;&#8199;&#847;</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="bg" bgcolor="{PAGE}" style="background:{PAGE}"><tr><td align="center" class="wrap" style="padding:36px 16px">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:560px">
<tr><td style="padding:0 4px 18px"><table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
<td style="vertical-align:middle"><img src="{LOGO}" width="36" height="36" alt="" style="display:block;border:0;border-radius:8px"></td>
<td style="vertical-align:middle;padding-left:10px;font-family:{FONT};font-size:20px;font-weight:700;color:{INK}" class="ink">Aloof</td></tr></table></td></tr>
<tr><td class="card pad" bgcolor="{CARD}" style="background:{CARD};border:1px solid {LINE};border-top:4px solid {ACC};border-radius:12px;padding:36px 36px 28px">
<h1 class="h1" style="margin:0 0 18px;font-size:24px;line-height:1.25;font-weight:700;color:{INK}">{e(title)}</h1>
{"".join(blocks)}</td></tr>
<tr><td style="padding:20px 8px 0;font-size:12px;line-height:1.6;color:{MUTED};font-family:{FONT}" class="muted">{foot}<br>{e(LEGAL)}</td></tr>
</table></td></tr></table></body></html>"""

def text(title, lines, reason, unsub=None):
    out = [title, "=" * min(len(title), 60), ""] + lines + ["", "--", reason + (f" Unsubscribe: {unsub}" if unsub else ""), LEGAL]
    return "\n".join(out) + "\n"

# ---- the emails ----
def magic_link(url, minutes=15):
    t = "Sign in to Aloof"
    reason = "You're getting this because someone asked for a sign-in link for this address."
    exp = f"This link works once and expires in {minutes} minutes."
    ign = "If you didn't ask for it, you can ignore this email. Nothing happens unless the link is opened."
    h = render(t, [para("Click the button to sign in. If this is your first time, we'll create your account."), button("Sign in to Aloof", url), small(exp), small(ign)], reason, "Your sign-in link works once and expires in %d minutes." % minutes)
    return "Your Aloof sign-in link", text(t, ["Open this link to sign in. If this is your first time, we'll create your account.", "", url, "", exp, ign], reason), h

def welcome(api_key, dash_url):
    t = "Your Aloof account is ready"
    reason = "You're getting this because an Aloof account was created with this address."
    h = render(t, [para("You can deploy your first app from the dashboard. Your API key is below. We only show it once, so copy it somewhere safe."), code(api_key),
                   button("Open the dashboard", dash_url), small("Lost the key? Create a new one under API keys in the dashboard.")], reason, "Your API key is shown once.")
    return "Welcome to Aloof", text(t, ["Your API key (shown once, copy it somewhere safe):", "", api_key, "", "Open the dashboard:", dash_url], reason), h

def low_balance(kind, bal, rate, when, link, threshold=None):
    s = "https://www.aloof.si/docs/support"
    if kind == "suspended":
        t = "Your apps are suspended"; subj = "Your Aloof apps are suspended: prepaid credit used up"
        blocks = [para(f"Your prepaid balance has reached {e(bal)}, so your running apps were scaled to zero and suspended."),
                  para("Volumes, databases and backups are kept, and they keep being billed. Add credit and your apps resume on their own."),
                  button("Add credit", link), small(f'Think this is a mistake? Contact us from the <a href="{s}" style="color:{ACC_DK}" class="lnk">Support page</a>.')]
        lines = [f"Your prepaid balance has reached {bal}, so your running apps were scaled to zero and suspended.", "Volumes, databases and backups are kept, and they keep being billed. Add credit and your apps resume on their own.", "", "Add credit:", link, "", f"Think this is a mistake? {s}"]
    else:
        head = {"72h": "about 3 days", "24h": "less than 24 hours"}.get(kind)
        t = f"Your credit lasts {head}" if head else "Your credit is below your alert"
        subj = f"Aloof credit: {head} left" if head else f"Aloof credit is below your ${threshold:g} alert"
        blocks = [para(f"Your prepaid balance is <b>{e(bal)}</b>. At your current usage ({e(rate)}) that lasts {e(when)}."),
                  para("When it reaches $0, running apps are scaled to zero and suspended until you add credit. Volumes and databases are kept."),
                  button("Add credit", link), small("You can turn these emails off or set your own threshold under Settings in the dashboard.")]
        lines = [f"Your prepaid balance is {bal}. At your current usage ({rate}) that lasts {when}.", "When it reaches $0, running apps are scaled to zero and suspended until you add credit. Volumes and databases are kept.", "", "Add credit:", link, "", "You can turn these emails off or set your own threshold under Settings in the dashboard."]
    reason = "You're getting this because you have an Aloof account with a prepaid balance."
    return subj, text(t, lines, reason), render(t, blocks, reason, f"Balance {bal}, {rate}.")

def waitlist(kind, what, body, unsub):
    t = {"design_partner": "We got your application", "region": "You're on the region waitlist"}.get(kind, "You're on the GPU waitlist")
    reason = "You're getting this because this address was entered on aloof.si."
    h = render(t, [para(f"You're on {e(what)}."), para(e(body)), small("If you didn't sign up, or you want to be removed, use the link below. It takes one click.")], reason, body, unsub=unsub)
    return text(t, [f"You're on {what}.", "", body, "", f"If you didn't sign up, or want to be removed: {unsub}"], reason), h
