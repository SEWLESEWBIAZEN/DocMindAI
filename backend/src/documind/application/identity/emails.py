"""Transactional email content. All interpolated user data is HTML-escaped."""

from __future__ import annotations

from html import escape
from urllib.parse import urlencode

from documind.application.ports import EmailMessage


def _link(base_url: str, path: str, token: str) -> str:
    return f"{base_url.rstrip('/')}{path}?{urlencode({'token': token})}"


def _html(
    title: str, body_html: str, button_text: str | None = None, url: str | None = None
) -> str:
    button = (
        f'<p><a href="{escape(url, quote=True)}" style="display:inline-block;padding:10px 18px;'
        f'background:#0e7490;color:#fff;border-radius:8px;text-decoration:none">'
        f"{escape(button_text)}</a></p>"
        if button_text and url
        else ""
    )
    return (
        '<div style="font-family:system-ui,sans-serif;max-width:520px;color:#18181b">'
        f"<h2>{escape(title)}</h2>{body_html}{button}"
        '<p style="color:#71717a;font-size:13px">'
        "If you did not expect this email, you can ignore it.</p>"
        "</div>"
    )


def verification_email(
    to: str, name: str, base_url: str, token: str, ttl_hours: int
) -> EmailMessage:
    url = _link(base_url, "/verify-email", token)
    return EmailMessage(
        to=to,
        subject="Verify your DocuMind email",
        text=f"Hi {name},\n\nConfirm your email address to finish setting up DocuMind:\n{url}\n\n"
        f"This link expires in {ttl_hours} hours.",
        html=_html(
            "Verify your email",
            f"<p>Hi {escape(name)},</p><p>Confirm your email address to finish setting up "
            f"DocuMind. This link expires in {ttl_hours} hours.</p>",
            "Verify email",
            url,
        ),
    )


def account_exists_email(to: str, name: str, base_url: str) -> EmailMessage:
    url = f"{base_url.rstrip('/')}/login"
    return EmailMessage(
        to=to,
        subject="Someone tried to sign up with your email",
        text=f"Hi {name},\n\nSomeone tried to create a DocuMind account with this email, but you "
        f"already have one. If this was you, sign in or reset your password:\n{url}",
        html=_html(
            "You already have an account",
            f"<p>Hi {escape(name)},</p><p>Someone tried to create a DocuMind account with this "
            "email, but you already have one. If this was you, sign in or reset your password.</p>",
            "Sign in",
            url,
        ),
    )


def password_reset_email(
    to: str, name: str, base_url: str, token: str, ttl_minutes: int
) -> EmailMessage:
    url = _link(base_url, "/reset-password", token)
    return EmailMessage(
        to=to,
        subject="Reset your DocuMind password",
        text=f"Hi {name},\n\nReset your password using this link (valid for {ttl_minutes} "
        f"minutes):\n{url}",
        html=_html(
            "Reset your password",
            f"<p>Hi {escape(name)},</p><p>Use the button below to choose a new password. "
            f"The link is valid for {ttl_minutes} minutes and can be used once.</p>",
            "Reset password",
            url,
        ),
    )


def password_changed_email(to: str, name: str) -> EmailMessage:
    return EmailMessage(
        to=to,
        subject="Your DocuMind password was changed",
        text=f"Hi {name},\n\nYour password was just changed and other sessions were signed out. "
        "If this wasn't you, reset your password immediately.",
        html=_html(
            "Password changed",
            f"<p>Hi {escape(name)},</p><p>Your password was just changed and your other sessions "
            "were signed out. If this was not you, reset your password immediately.</p>",
        ),
    )


def invitation_email(
    to: str, inviter: str, workspace: str, role: str, base_url: str, token: str, ttl_days: int
) -> EmailMessage:
    url = _link(base_url, "/invitations/accept", token)
    return EmailMessage(
        to=to,
        subject=f"{inviter} invited you to {workspace} on DocuMind",
        text=f"{inviter} invited you to join {workspace} on DocuMind as {role}.\n\n"
        f"Accept the invitation (valid for {ttl_days} days):\n{url}\n\n"
        "Sign in or create an account with this email address to accept.",
        html=_html(
            f"Join {workspace}",
            f"<p><strong>{escape(inviter)}</strong> invited you to join "
            f"<strong>{escape(workspace)}</strong> on DocuMind as {escape(role)}.</p>"
            f"<p>Sign in or create an account with this email address to accept. "
            f"The invitation is valid for {ttl_days} days.</p>",
            "Accept invitation",
            url,
        ),
    )
