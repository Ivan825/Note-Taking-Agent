from notes_common.states import PIPELINE, TERMINAL, Status, can_transition, stage_index


def test_happy_path_order():
    order = [Status.QUEUED, *PIPELINE, Status.COMPLETED]
    for a, b in zip(order, order[1:]):
        assert can_transition(a, b), f"{a} -> {b}"


def test_cannot_skip_or_go_back():
    assert not can_transition(Status.QUEUED, Status.SUMMARIZING)
    assert not can_transition(Status.PUBLISHING, Status.TRANSCRIBING)
    assert not can_transition(Status.TRANSCRIBING, Status.COMPLETED)


def test_any_non_terminal_can_fail_or_dead_letter():
    for s in [Status.QUEUED, *PIPELINE]:
        assert can_transition(s, Status.FAILED)
        assert can_transition(s, Status.DEAD_LETTERED)


def test_terminal_states_are_final():
    for t in TERMINAL:
        for s in Status:
            assert not can_transition(t, s)


def test_reentering_stage_allowed_for_resume():
    for s in PIPELINE:
        assert can_transition(s, s)


def test_with_warnings_only_from_publishing():
    assert can_transition(Status.PUBLISHING, Status.COMPLETED_WITH_WARNINGS)
    assert not can_transition(Status.EXTRACTING_FRAMES, Status.COMPLETED_WITH_WARNINGS)


def test_stage_index():
    assert stage_index(Status.QUEUED) == -1
    assert stage_index(Status.TRANSCRIBING) == 0
    assert stage_index(Status.COMPLETED) == len(PIPELINE)
