"""HTML pages served by the OAuth callback server.

Ported from `pi-mono/packages/ai/src/utils/oauth/oauth-page.ts`. Pure
HTML/CSS, no external assets — the user sees this for ~1 second after
approving in the browser, so it must work fully offline.
"""

from __future__ import annotations

from html import escape

_LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 800" '
    'aria-hidden="true">'
    '<path fill="#fff" fill-rule="evenodd" '
    'd="M165.29 165.29 H517.36 V400 H400 V517.36 H282.65 V634.72 '
    'H165.29 Z M282.65 282.65 V400 H400 V282.65 Z"/>'
    '<path fill="#fff" '
    'd="M517.36 400 H634.72 V634.72 H517.36 Z"/>'
    "</svg>"
)

# Pre-built font stacks. The literals are intentionally one long line each
# so the rendered HTML matches pi-mono byte-for-byte.
# fmt: off
_FONT_SANS = 'ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", sans-serif, "Apple Color Emoji", "Segoe UI Emoji", "Segoe UI Symbol", "Noto Color Emoji"'  # noqa: E501
_FONT_MONO = 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace'  # noqa: E501
# fmt: on
_H1_RULE = (
    "h1 {{ margin: 0 0 10px; font-size: 28px; line-height: 1.15; "
    "font-weight: 650; color: var(--text); }}"
)


def _render_page(
    *,
    title: str,
    heading: str,
    message: str,
    details: str | None = None,
) -> str:
    """Render the shared dark-theme OAuth result page."""
    title_e = escape(title)
    heading_e = escape(heading)
    message_e = escape(message)
    details_block = f'<div class="details">{escape(details)}</div>' if details else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title_e}</title>
  <style>
    :root {{
      --text: #fafafa;
      --text-dim: #a1a1aa;
      --page-bg: #09090b;
      --font-sans: {_FONT_SANS};
      --font-mono: {_FONT_MONO};
    }}
    * {{ box-sizing: border-box; }}
    html {{ color-scheme: dark; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
      background: var(--page-bg);
      color: var(--text);
      font-family: var(--font-sans);
      text-align: center;
    }}
    main {{
      width: 100%;
      max-width: 560px;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
    }}
    .logo {{ width: 72px; height: 72px; display: block; margin-bottom: 24px; }}
    {_H1_RULE}
    p {{ margin: 0; line-height: 1.7; color: var(--text-dim); font-size: 15px; }}
    .details {{
      margin-top: 16px;
      font-family: var(--font-mono);
      font-size: 13px;
      color: var(--text-dim);
      white-space: pre-wrap;
      word-break: break-word;
    }}
  </style>
</head>
<body>
  <main>
    <div class="logo">{_LOGO_SVG}</div>
    <h1>{heading_e}</h1>
    <p>{message_e}</p>
    {details_block}
  </main>
</body>
</html>"""


def oauth_success_html(message: str) -> str:
    """Return the HTML body for a successful OAuth callback."""
    return _render_page(
        title="Authentication successful",
        heading="Authentication successful",
        message=message,
    )


def oauth_error_html(message: str, details: str | None = None) -> str:
    """Return the HTML body for a failed OAuth callback."""
    return _render_page(
        title="Authentication failed",
        heading="Authentication failed",
        message=message,
        details=details,
    )
