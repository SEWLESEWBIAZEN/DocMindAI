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


def mfa_enabled_email(to: str, name: str) -> EmailMessage:
    return EmailMessage(
        to=to,
        subject="Two-factor authentication is on",
        text=f"Hi {name},\n\nTwo-factor authentication was just turned on for your DocuMind "
        "account. Keep your recovery codes somewhere safe. If this wasn't you, reset your "
        "password immediately.",
        html=_html(
            "Two-factor authentication is on",
            f"<p>Hi {escape(name)},</p><p>Two-factor authentication was just turned on for your "
            "account. Keep your recovery codes somewhere safe.</p>",
        ),
    )


def mfa_disabled_email(to: str, name: str) -> EmailMessage:
    return EmailMessage(
        to=to,
        subject="Two-factor authentication was turned off",
        text=f"Hi {name},\n\nTwo-factor authentication was just turned off for your DocuMind "
        "account. If this wasn't you, reset your password immediately.",
        html=_html(
            "Two-factor authentication was turned off",
            f"<p>Hi {escape(name)},</p><p>Two-factor authentication was just turned off for your "
            "account. If this was not you, reset your password immediately.</p>",
        ),
    )


def recovery_code_used_email(to: str, name: str, remaining: int) -> EmailMessage:
    return EmailMessage(
        to=to,
        subject="A recovery code was used on your account",
        text=f"Hi {name},\n\nA two-factor recovery code was just used. You have {remaining} "
        "left. If this wasn't you, reset your password and regenerate your codes.",
        html=_html(
            "A recovery code was used",
            f"<p>Hi {escape(name)},</p><p>A two-factor recovery code was just used. You have "
            f"{remaining} left. If this was not you, reset your password and regenerate "
            "your codes.</p>",
        ),
    )


def mfa_email_code_email(to: str, name: str, code: str, minutes: int) -> EmailMessage:
    spaced = f"{code[:3]} {code[3:]}"
    return EmailMessage(
        to=to,
        subject=f"Your DocuMind sign-in code: {spaced}",
        text=f"Hi {name},\n\nYour one-time code is {spaced}. It works once and expires in "
        f"{minutes} minutes.\n\nIf you didn't ask for it, someone may know your password: "
        "change it now. Never share this code; DocuMind will never ask you for it.",
        html=_html(
            "Your one-time code",
            f"<p>Hi {escape(name)},</p><p>Your one-time code is</p>"
            f'<p style="font-size:28px;font-weight:700;letter-spacing:4px">{spaced}</p>'
            f"<p>It works once and expires in {minutes} minutes. If you didn't ask for it, "
            "someone may know your password: change it now. Never share this code.</p>",
        ),
    )


def mfa_email_code_used_email(to: str, name: str) -> EmailMessage:
    return EmailMessage(
        to=to,
        subject="An email code was used on your account",
        text=f"Hi {name},\n\nA one-time code sent to this address was just used instead of "
        "your authenticator app. If this wasn't you, change your password and your email "
        "password now.",
        html=_html(
            "An email code was used",
            f"<p>Hi {escape(name)},</p><p>A one-time code sent to this address was just used "
            "instead of your authenticator app. If this was not you, change your password and "
            "your email password now.</p>",
        ),
    )


def provider_key_rejected_email(
    to: str, name: str, base_url: str, workspace_id: str, workspace_name: str, provider: str
) -> EmailMessage:
    url = f"{base_url.rstrip('/')}/w/{workspace_id}/ai-providers"
    return EmailMessage(
        to=to,
        subject=f"Action needed: the AI key for {workspace_name} stopped working",
        text=f"Hi {name},\n\n{provider} rejected the API key saved for {workspace_name} "
        "(it may have been revoked, expired or run out of quota). AI features that use it are "
        "paused; DocuMind does not switch to another key.\n\n"
        f"Update the key under AI providers:\n{url}",
        html=_html(
            "Your AI provider key stopped working",
            f"<p>Hi {escape(name)},</p><p>{escape(provider)} rejected the API key saved for "
            f"<strong>{escape(workspace_name)}</strong>. It may have been revoked, expired or run "
            "out of quota. AI features that use it are paused; DocuMind doesn't switch to "
            "another key.</p>",
            "Update the key",
            url,
        ),
    )
