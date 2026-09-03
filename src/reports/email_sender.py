"""
SMTP email delivery for scheduled reports.

Configuration via environment variables:

    SMTP_HOST      SMTP server hostname          (default: localhost)
    SMTP_PORT      SMTP port                     (default: 587)
    SMTP_USER      SMTP username / API key login (optional)
    SMTP_PASSWORD  SMTP password / API key       (optional)
    SMTP_FROM      Sender address                (default: reports@noesis.local)
    SMTP_TLS       Use STARTTLS                  (default: true)
    SMTP_SSL       Use SMTP_SSL instead          (default: false)
    BASE_URL       Public API base for tracking pixel (default: http://localhost:8000)

Provider quick-start
--------------------
SendGrid:   SMTP_HOST=smtp.sendgrid.net SMTP_PORT=587 SMTP_USER=apikey SMTP_PASSWORD=<key>
Mailgun:    SMTP_HOST=smtp.mailgun.org  SMTP_PORT=587 SMTP_USER=<login> SMTP_PASSWORD=<key>
Postfix:    SMTP_HOST=localhost SMTP_PORT=25  (no auth, SMTP_TLS=false)
"""

from __future__ import annotations

import logging
import os
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

logger = logging.getLogger(__name__)


def _cfg(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _send_raw(msg: MIMEMultipart) -> None:
    host = _cfg("SMTP_HOST", "localhost")
    port = int(_cfg("SMTP_PORT", "587"))
    user = _cfg("SMTP_USER")
    password = _cfg("SMTP_PASSWORD")
    use_ssl = _cfg("SMTP_SSL", "false").lower() == "true"
    use_tls = _cfg("SMTP_TLS", "true").lower() == "true" and not use_ssl

    cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with cls(host, port) as server:
        if use_tls:
            server.starttls()
        if user and password:
            server.login(user, password)
        server.send_message(msg)


def send_report_email(
    *,
    to: str,
    topic: str,
    period: str,
    frequency: str,
    fmt: str,
    report_bytes: bytes,
    tracking_token: str,
) -> None:
    """
    Send a report email with the report attached.

    fmt must be 'pdf' or 'csv'. tracking_token is embedded as a 1×1 pixel
    in the HTML body so open events can be recorded.
    """
    from_addr = _cfg("SMTP_FROM", "reports@noesis.local")
    base_url = _cfg("BASE_URL", "http://localhost:8000")
    tracking_url = f"{base_url}/api/v1/reports/track/open/{tracking_token}"

    subject = f"[Noesis] {frequency.capitalize()} report: {topic} ({period.replace('_', ' ')})"

    html = f"""
<html><body style="font-family:sans-serif;color:#222;max-width:600px;margin:auto">
  <h2 style="color:#3B6EEA">Noesis Report: {topic}</h2>
  <p>Your <strong>{frequency}</strong> report for <em>{topic}</em>
     covering <em>{period.replace('_', ' ')}</em> is attached.</p>
  <p style="color:#666;font-size:12px">
    To unsubscribe, contact your Noesis administrator.<br>
    Powered by <a href="{base_url}">Noesis Knowledge Engine</a>
  </p>
  <img src="{tracking_url}" width="1" height="1" alt="" style="display:none">
</body></html>
"""
    text = (
        f"Noesis Report: {topic}\n\n"
        f"Your {frequency} report for '{topic}' covering {period.replace('_', ' ')} is attached.\n"
    )

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text, "plain"))
    alt.attach(MIMEText(html, "html"))
    msg.attach(alt)

    # Attach report file
    if fmt == "pdf":
        mime_type, ext = "application/pdf", "pdf"
    else:
        mime_type, ext = "text/csv", "csv"

    slug = topic.lower().replace(" ", "_")
    filename = f"noesis_report_{slug}_{period}.{ext}"

    part = MIMEBase(*mime_type.split("/"))
    part.set_payload(report_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
    msg.attach(part)

    _send_raw(msg)
    logger.info("Report email sent to %s (topic=%s token=%s)", to, topic, tracking_token)


def send_markdown_email(*, to: str, subject: str, markdown: str) -> None:
    """Send a plain-text/HTML-safe Markdown brief without an attachment.

    The HTML part deliberately wraps escaped text rather than interpreting
    user- or source-controlled Markdown as HTML.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = _cfg("SMTP_FROM", "reports@noesis.local")
    msg["To"] = to
    msg.attach(MIMEText(markdown, "plain", "utf-8"))
    safe = escape(markdown)
    msg.attach(MIMEText(
        f'<html><body><pre style="white-space:pre-wrap;font-family:sans-serif">{safe}</pre></body></html>',
        "html", "utf-8",
    ))
    _send_raw(msg)
    logger.info("Daily brief sent to %s", to)
