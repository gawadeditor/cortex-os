"""
hypogeum/email_templates.py — the emails Cortex sends, in the dashboard's own visual language.

Dark layered surfaces, one ember accent (#f0a838), the CORTEX wordmark with its glowing square, system fonts.
Email clients are not browsers, so everything is table-based with inline styles and explicit bgcolor attributes
(Outlook ignores CSS backgrounds); `color-scheme: dark` keeps clients that auto-invert from fighting the palette.
Every email is multipart: a plain-text part (what spam filters and screen readers like) and the HTML.

    sign_in(link)                                  -> (subject, text, html)
    invitation(workspace_name, link, portal_url)   -> (subject, text, html)
    alert(text, portal_url)                        -> (subject, text, html)
"""
from __future__ import annotations

from html import escape

BG = "#090b11"
SURFACE = "#14171d"
RAISED = "#1b1f27"
BORDER = "#262b33"
TEXT = "#f4f6f9"
MUTED = "#a4aebd"
FAINT = "#6b7481"
ACCENT = "#f0a838"
ACCENT_INK = "#1a1206"
CRITICAL = "#f0574a"
GOOD = "#4ec26f"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','Helvetica Neue',Arial,sans-serif"


def _button(label: str, url: str) -> str:
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:26px 0 8px"><tr>'
            f'<td bgcolor="{ACCENT}" style="border-radius:9px"><a href="{escape(url, quote=True)}" '
            f'style="display:inline-block;padding:13px 26px;font:700 15px {FONT};color:{ACCENT_INK};text-decoration:none;'
            f'border-radius:9px">{escape(label)}</a></td></tr></table>')


def _shell(preheader: str, title: str, body_html: str, footer_note: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><meta name="supported-color-schemes" content="dark">
<title>{escape(title)}</title></head>
<body style="margin:0;padding:0;background:{BG}" bgcolor="{BG}">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:{BG}">{escape(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{BG}" style="background:{BG}">
<tr><td align="center" style="padding:36px 16px">
  <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:560px">
    <tr><td style="padding:0 4px 22px">
      <span style="font:800 15px {FONT};letter-spacing:.09em;text-transform:uppercase;color:{TEXT}"><span style="color:{ACCENT};font-size:13px">&#9632;</span>&nbsp;&nbsp;Cortex</span>
    </td></tr>
    <tr><td bgcolor="{RAISED}" style="background:{RAISED};border:1px solid {BORDER};border-radius:14px;padding:34px 32px;
        font:15px/1.65 {FONT};color:{TEXT}">
      {body_html}
    </td></tr>
    <tr><td style="padding:20px 6px 0;font:12px/1.6 {FONT};color:{FAINT}">{footer_note}<br>Cortex Channels · cortexchannels.com</td></tr>
  </table>
</td></tr></table></body></html>"""


def _h1(t: str) -> str:
    return f'<h1 style="margin:0 0 12px;font:700 26px/1.2 {FONT};letter-spacing:-.02em;color:{TEXT}">{escape(t)}</h1>'


def _p(t: str, muted: bool = False) -> str:
    return f'<p style="margin:0 0 14px;color:{MUTED if muted else TEXT}">{escape(t)}</p>'


def _fallback(url: str) -> str:
    return (f'<p style="margin:18px 0 0;font:12px/1.6 {FONT};color:{FAINT}">Button not working? Copy this address into your browser:<br>'
            f'<a href="{escape(url, quote=True)}" style="color:{ACCENT};word-break:break-all">{escape(url)}</a></p>')


def sign_in(link: str):
    subject = "Your Cortex sign-in link"
    text = (f"Open this link to sign in to Cortex (valid 15 minutes, single use):\n\n{link}\n\n"
            "If you did not ask for it, you can ignore this email.\n")
    body = (_h1("Sign in to Cortex") + _p("Use the button below to open your portal. It works once and expires in 15 minutes.", True)
            + _button("Sign in", link) + _fallback(link))
    return subject, text, _shell("Your one-time sign-in link", subject, body,
                                 "You received this because someone asked to sign in with this address. If it was not you, ignore it.")


def invitation(workspace_name: str, link: str, portal_url: str):
    subject = f"You have been invited to Cortex ({workspace_name})"
    text = (f"You now have access to the Cortex portal for {workspace_name}: your channels, what we have published for you, "
            f"this month's spending and anything that needs your attention.\n\nSign in with this link (valid 7 days, single use):\n\n{link}\n\n"
            f"After that, request a new sign-in link any time from {portal_url}/login with this email address.\n")
    body = (_h1(f"Welcome to {workspace_name}") +
            _p("You now have your own Cortex portal: your channels, everything we have published for you, this month's spending, "
               "and anything that needs your attention.", True)
            + _p("Your first step is a short setup, a few minutes to connect your keys and choose how you want to hear from us.", True)
            + _button("Open your portal", link)
            + f'<p style="margin:6px 0 0;color:{MUTED};font-size:13px">This link is valid for 7 days and works once. After that, '
              f'ask for a fresh one any time at <a href="{escape(portal_url, quote=True)}/login" style="color:{ACCENT}">'
              f'{escape(portal_url)}/login</a>.</p>' + _fallback(link))
    return subject, text, _shell(f"Your Cortex portal for {workspace_name} is ready", subject, body,
                                 "You received this because the Cortex team added you to this workspace.")


def alert(text_body: str, portal_url: str):
    lines = [ln.strip() for ln in text_body.strip().splitlines() if ln.strip()]
    head = (lines[0] if lines else "Cortex notification").lstrip("🔴✅🛠 ")
    rest = lines[1:]
    resolved = text_body.strip().startswith("✅")
    colour = GOOD if resolved else CRITICAL
    subject = head[:120]
    plain = text_body.strip() + f"\n\n—\nOpen your portal: {portal_url}\nTo stop these emails, turn off 'Email me about problems' there.\n"
    rows = "".join(f'<p style="margin:0 0 8px;color:{MUTED}">{escape(r)}</p>' for r in rest)
    body = (f'<p style="margin:0 0 16px;font:700 11px {FONT};letter-spacing:.09em;text-transform:uppercase;color:{colour}">'
            f'&#9679;&nbsp;&nbsp;{"Resolved" if resolved else "Needs attention"}</p>'
            + _h1(head) + rows + _button("Open your portal", portal_url))
    return subject, plain, _shell(head, subject, body, "You can turn these emails off in your portal, under Notifications.")
