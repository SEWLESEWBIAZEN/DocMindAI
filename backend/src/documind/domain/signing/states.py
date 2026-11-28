"""Agreement and recipient state machines (design §5).

States only change through :func:`next_agreement_status` and
:func:`next_recipient_status`; services call them while holding the agreement row lock.
"""

from __future__ import annotations

from enum import StrEnum

from documind.domain.errors import ConflictError


class AgreementStatus(StrEnum):
    DRAFT = "draft"
    SENT = "sent"
    PARTIALLY_SIGNED = "partially_signed"
    #: Everyone has acted; the signed PDF is being produced ("Completing…").
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    DECLINED = "declined"
    VOIDED = "voided"
    EXPIRED = "expired"
    #: Recoverable: an operator re-queues finalization.
    FINALIZATION_FAILED = "finalization_failed"


class AgreementEvent(StrEnum):
    SEND = "send"
    RECIPIENT_COMPLETED = "recipient_completed"  # others still to act
    ALL_COMPLETED = "all_completed"
    FINALIZED = "finalized"
    FINALIZE_FAILED = "finalize_failed"
    RETRY_FINALIZE = "retry_finalize"
    DECLINE = "decline"
    VOID = "void"
    EXPIRE = "expire"


class RecipientStatus(StrEnum):
    PENDING = "pending"
    INVITED = "invited"
    VIEWED = "viewed"
    CONSENTED = "consented"
    SIGNED = "signed"
    APPROVED = "approved"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class RecipientEvent(StrEnum):
    ACTIVATE = "activate"  # their group's turn
    OPEN = "open"
    CONSENT = "consent"
    COMPLETE = "complete"
    DECLINE = "decline"
    CANCEL = "cancel"
    EXPIRE = "expire"


class RecipientRole(StrEnum):
    SIGNER = "signer"
    APPROVER = "approver"


_A = AgreementStatus
_E = AgreementEvent
_ACTIVE = (_A.SENT, _A.PARTIALLY_SIGNED)

AGREEMENT_TRANSITIONS: dict[tuple[AgreementStatus, AgreementEvent], AgreementStatus] = {
    (_A.DRAFT, _E.SEND): _A.SENT,
    (_A.SENT, _E.RECIPIENT_COMPLETED): _A.PARTIALLY_SIGNED,
    (_A.PARTIALLY_SIGNED, _E.RECIPIENT_COMPLETED): _A.PARTIALLY_SIGNED,
    **{(s, _E.ALL_COMPLETED): _A.FINALIZING for s in _ACTIVE},
    **{(s, _E.DECLINE): _A.DECLINED for s in _ACTIVE},
    **{(s, _E.VOID): _A.VOIDED for s in _ACTIVE},
    **{(s, _E.EXPIRE): _A.EXPIRED for s in _ACTIVE},
    (_A.FINALIZING, _E.FINALIZED): _A.COMPLETED,
    (_A.FINALIZING, _E.FINALIZE_FAILED): _A.FINALIZATION_FAILED,
    (_A.FINALIZATION_FAILED, _E.RETRY_FINALIZE): _A.FINALIZING,
}

TERMINAL = frozenset({_A.COMPLETED, _A.DECLINED, _A.VOIDED, _A.EXPIRED})
#: Recipients may open the portal and act only in these states.
SIGNABLE = frozenset(_ACTIVE)

_R = RecipientStatus
_RE = RecipientEvent
_OPEN = (_R.INVITED, _R.VIEWED, _R.CONSENTED)

RECIPIENT_TRANSITIONS: dict[tuple[RecipientStatus, RecipientEvent], RecipientStatus] = {
    (_R.PENDING, _RE.ACTIVATE): _R.INVITED,
    (_R.INVITED, _RE.OPEN): _R.VIEWED,
    (_R.VIEWED, _RE.OPEN): _R.VIEWED,
    (_R.CONSENTED, _RE.OPEN): _R.CONSENTED,
    (_R.VIEWED, _RE.CONSENT): _R.CONSENTED,
    **{(s, _RE.DECLINE): _R.DECLINED for s in _OPEN},
    **{(s, _RE.CANCEL): _R.CANCELLED for s in (_R.PENDING, *_OPEN)},
    **{(s, _RE.EXPIRE): _R.EXPIRED for s in (_R.PENDING, *_OPEN)},
}

RECIPIENT_DONE = frozenset({_R.SIGNED, _R.APPROVED})
RECIPIENT_FINAL = frozenset({*RECIPIENT_DONE, _R.DECLINED, _R.CANCELLED, _R.EXPIRED})


def next_agreement_status(current: AgreementStatus, event: AgreementEvent) -> AgreementStatus:
    try:
        return AGREEMENT_TRANSITIONS[(current, event)]
    except KeyError:
        state, action = current.value.replace("_", " "), event.value.replace("_", " ")
        raise ConflictError(
            f"An agreement that is {state} can't {action}.", code="invalid_agreement_state"
        ) from None


def next_recipient_status(
    current: RecipientStatus, event: RecipientEvent, role: RecipientRole
) -> RecipientStatus:
    if event is _RE.COMPLETE:
        if current is not _R.CONSENTED:
            raise ConflictError(
                "Accept the electronic signature disclosure first.", code="consent_required"
            )
        return _R.SIGNED if role is RecipientRole.SIGNER else _R.APPROVED
    try:
        return RECIPIENT_TRANSITIONS[(current, event)]
    except KeyError:
        raise ConflictError(
            f"This step isn't available ({current.value}).", code="invalid_recipient_state"
        ) from None
