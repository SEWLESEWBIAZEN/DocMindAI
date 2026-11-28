"""DocuMind Sign domain rules: state machines, signing order, field coordinates."""

from __future__ import annotations

import itertools

import pytest

from documind.domain.errors import ConflictError, ValidationFailedError
from documind.domain.signing.fields import (
    FieldBox,
    PageGeometry,
    normalise_rotation,
    to_pdf_point,
    to_pdf_rect,
    validate_box,
)
from documind.domain.signing.routing import (
    RoutedRecipient,
    Routing,
    active_group,
    all_done,
    may_act,
    normalise_orders,
)
from documind.domain.signing.states import (
    AGREEMENT_TRANSITIONS,
    TERMINAL,
    AgreementEvent,
    AgreementStatus,
    RecipientEvent,
    RecipientRole,
    RecipientStatus,
    next_agreement_status,
    next_recipient_status,
)

# --------------------------------------------------------------------------- states


@pytest.mark.parametrize(
    ("state", "event"), list(itertools.product(AgreementStatus, AgreementEvent))
)
def test_every_agreement_pair_is_either_allowed_or_refused(
    state: AgreementStatus, event: AgreementEvent
) -> None:
    expected = AGREEMENT_TRANSITIONS.get((state, event))
    if expected is None:
        with pytest.raises(ConflictError):
            next_agreement_status(state, event)
    else:
        assert next_agreement_status(state, event) is expected


def test_terminal_states_accept_nothing() -> None:
    for state, event in itertools.product(TERMINAL, AgreementEvent):
        with pytest.raises(ConflictError):
            next_agreement_status(state, event)


def test_key_rules() -> None:
    # A declined agreement never completes; a voided one takes no more signatures.
    with pytest.raises(ConflictError):
        next_agreement_status(AgreementStatus.DECLINED, AgreementEvent.ALL_COMPLETED)
    with pytest.raises(ConflictError):
        next_agreement_status(AgreementStatus.VOIDED, AgreementEvent.RECIPIENT_COMPLETED)
    # Nothing can void or expire while the signed PDF is being produced.
    for event in (AgreementEvent.VOID, AgreementEvent.EXPIRE, AgreementEvent.DECLINE):
        with pytest.raises(ConflictError):
            next_agreement_status(AgreementStatus.FINALIZING, event)
    # Finalization failure is recoverable.
    failed = next_agreement_status(AgreementStatus.FINALIZING, AgreementEvent.FINALIZE_FAILED)
    assert (
        next_agreement_status(failed, AgreementEvent.RETRY_FINALIZE) is AgreementStatus.FINALIZING
    )


def test_recipient_must_consent_before_completing() -> None:
    with pytest.raises(ConflictError) as err:
        next_recipient_status(RecipientStatus.VIEWED, RecipientEvent.COMPLETE, RecipientRole.SIGNER)
    assert err.value.code == "consent_required"
    assert (
        next_recipient_status(
            RecipientStatus.CONSENTED, RecipientEvent.COMPLETE, RecipientRole.SIGNER
        )
        is RecipientStatus.SIGNED
    )
    assert (
        next_recipient_status(
            RecipientStatus.CONSENTED, RecipientEvent.COMPLETE, RecipientRole.APPROVER
        )
        is RecipientStatus.APPROVED
    )


def test_a_signed_recipient_cannot_act_again() -> None:
    for event in RecipientEvent:
        with pytest.raises(ConflictError):
            next_recipient_status(RecipientStatus.SIGNED, event, RecipientRole.SIGNER)


def test_recipient_path() -> None:
    s = RecipientStatus.PENDING
    for event in (
        RecipientEvent.ACTIVATE,
        RecipientEvent.OPEN,
        RecipientEvent.OPEN,
        RecipientEvent.CONSENT,
    ):
        s = next_recipient_status(s, event, RecipientRole.SIGNER)
    assert s is RecipientStatus.CONSENTED
    with pytest.raises(ConflictError):  # consenting twice is not a new event
        next_recipient_status(s, RecipientEvent.CONSENT, RecipientRole.SIGNER)


# --------------------------------------------------------------------------- routing


def _r(i: int, group: int, status: RecipientStatus = RecipientStatus.INVITED) -> RoutedRecipient:
    return RoutedRecipient(id=i, group_order=group, status=status)


def test_order_normalisation() -> None:
    assert normalise_orders(Routing.SEQUENTIAL, [5, 5, 5]) == [1, 2, 3]
    assert normalise_orders(Routing.PARALLEL, [1, 2, 3]) == [1, 1, 1]
    assert normalise_orders(Routing.GROUPS, [3, 1, 3, 7]) == [2, 1, 2, 3]
    with pytest.raises(ValidationFailedError):
        normalise_orders(Routing.GROUPS, [0, 1])


def test_sequential_turns() -> None:
    a, b, c = _r(1, 1), _r(2, 2, RecipientStatus.PENDING), _r(3, 3, RecipientStatus.PENDING)
    everyone = [a, b, c]
    assert active_group(everyone) == 1
    assert may_act(a, everyone)
    assert not may_act(b, everyone)
    a = _r(1, 1, RecipientStatus.SIGNED)
    assert may_act(b, [a, b, c])
    assert not may_act(c, [a, b, c])


def test_parallel_and_groups() -> None:
    a, b = _r(1, 1), _r(2, 1)
    assert may_act(a, [a, b])
    assert may_act(b, [a, b])
    c = _r(3, 2, RecipientStatus.PENDING)
    assert not may_act(c, [a, b, c])
    done = [_r(1, 1, RecipientStatus.SIGNED), _r(2, 1, RecipientStatus.APPROVED)]
    assert may_act(c, [*done, c])
    assert not all_done([*done, c])
    assert all_done(done)
    assert active_group(done) is None


# --------------------------------------------------------------------------- coordinates

# An A4-ish page whose CropBox is offset from the origin, to catch offset mistakes.
CROP = (10.0, 20.0, 610.0, 820.0)  # 600 x 800


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_displayed_corners_map_to_the_cropbox(rotate: int) -> None:
    page = PageGeometry(index=0, cropbox=CROP, rotate=rotate)
    w, h = page.display_size
    assert (w, h) == ((800.0, 600.0) if rotate in (90, 270) else (600.0, 800.0))
    corners = {to_pdf_point(u, v, page) for u, v in [(0, 0), (w, 0), (0, h), (w, h)]}
    x0, y0, x1, y1 = CROP
    assert corners == {(x0, y0), (x0, y1), (x1, y0), (x1, y1)}


def test_displayed_top_left_is_where_a_reader_expects() -> None:
    """Rotation 0: top left of the display is the CropBox's top-left (x0, y1)."""
    assert to_pdf_point(0, 0, PageGeometry(0, CROP, 0)) == (10.0, 820.0)
    # /Rotate 90 turns the page clockwise: the display's top left is the page's bottom left.
    assert to_pdf_point(0, 0, PageGeometry(0, CROP, 90)) == (10.0, 20.0)
    assert to_pdf_point(0, 0, PageGeometry(0, CROP, 180)) == (610.0, 20.0)
    assert to_pdf_point(0, 0, PageGeometry(0, CROP, 270)) == (610.0, 820.0)


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_rectangles_keep_their_size(rotate: int) -> None:
    page = PageGeometry(0, CROP, rotate)
    x0, y0, x1, y1 = to_pdf_rect(FieldBox(0, 50, 100, 200, 40), page)
    size = (x1 - x0, y1 - y0)
    # Width and height swap in user space when the page is turned a quarter.
    assert size == ((40.0, 200.0) if rotate in (90, 270) else (200.0, 40.0))
    assert CROP[0] <= x0 <= x1 <= CROP[2]
    assert CROP[1] <= y0 <= y1 <= CROP[3]


def test_field_validation() -> None:
    pages = [PageGeometry(0, CROP, 0), PageGeometry(1, CROP, 90)]
    validate_box(FieldBox(0, 0, 0, 600, 800), pages)  # the whole page is fine
    validate_box(FieldBox(1, 700, 500, 100, 100), pages)  # landscape page is 800 wide
    for bad in (
        FieldBox(2, 0, 0, 50, 20),  # no such page
        FieldBox(0, 590, 0, 50, 20),  # sticks out on the right
        FieldBox(0, 0, 0, 4, 20),  # too small
        FieldBox(1, 0, 590, 50, 20),  # landscape page is only 600 high
    ):
        with pytest.raises(ValidationFailedError):
            validate_box(bad, pages)
    assert normalise_rotation(-90) == 270
    with pytest.raises(ValidationFailedError):
        normalise_rotation(45)
