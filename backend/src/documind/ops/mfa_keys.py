"""Re-encrypt stored 2FA secrets with the current MFA key (production readiness F4).

Rotation procedure (docs/runbooks/disaster-recovery.md#key-rotation):

1. Generate a new key; set it as ``MFA_ENCRYPTION_KEY`` and move the old one to
   ``MFA_ENCRYPTION_KEYS_PREVIOUS``; restart. Everything keeps working: secrets are
   decrypted with whichever key they name, new ones use the new key.
2. ``documind-manage mfa rewrap --dry-run``, then without ``--dry-run``.
3. Keep the old key in ``MFA_ENCRYPTION_KEYS_PREVIOUS`` while people still hold recovery
   codes or email codes issued under it (those are keyed hashes and can't be re-keyed).
   Retire it once everyone has regenerated their recovery codes, or accept that old
   recovery codes stop working.

Secrets are only ever held in memory for the re-encryption: nothing is logged or printed
but counts and user ids.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from documind.infrastructure.database.models import User
from documind.infrastructure.security.secret_box import SecretBox, SecretDecryptionError

BATCH = 200
_FIELDS = ("totp_secret_encrypted", "totp_pending_secret_encrypted")


@dataclass(slots=True)
class RewrapReport:
    current_key: str
    checked: int = 0
    rewrapped: int = 0
    already_current: int = 0
    undecryptable: list[uuid.UUID] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "current_key_id": self.current_key,
            "secrets_checked": self.checked,
            "rewrapped": self.rewrapped,
            "already_current": self.already_current,
            "undecryptable_user_ids": [str(u) for u in self.undecryptable],
        }


def _context(user: User) -> str:
    return f"totp:{user.id}"  # must match MfaService._context


async def rewrap(
    sessions: async_sessionmaker[AsyncSession], box: SecretBox, *, dry_run: bool
) -> RewrapReport:
    report = RewrapReport(current_key=box.primary_id)
    last: uuid.UUID | None = None
    while True:
        async with sessions() as session:
            stmt = (
                select(User)
                .where(or_(*(getattr(User, f).is_not(None) for f in _FIELDS)))
                .order_by(User.id)
                .limit(BATCH)
                .with_for_update(skip_locked=True)
            )
            if last is not None:
                stmt = stmt.where(User.id > last)
            users = list((await session.execute(stmt)).scalars())
            if not users:
                return report
            for user in users:
                last = user.id
                for name in _FIELDS:
                    token = getattr(user, name)
                    if token is None:
                        continue
                    report.checked += 1
                    if box.is_current(token):
                        report.already_current += 1
                        continue
                    try:
                        plaintext = box.decrypt(token, context=_context(user))
                    except SecretDecryptionError:
                        report.undecryptable.append(user.id)
                        continue
                    if not dry_run:
                        setattr(user, name, box.encrypt(plaintext, context=_context(user)))
                    del plaintext
                    report.rewrapped += 1
            if dry_run:
                await session.rollback()
            else:
                await session.commit()
