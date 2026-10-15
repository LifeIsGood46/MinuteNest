"""C14. Whisper's repeated short phrases are written once (TEST_PLAN.md).

Seen on the VM (tiny, a low-quality Russian meeting): "Прикой." six times in a
row, "Вот это." twice. Measured on four of his meetings, 12 runs: every such run
was 1-3 words, its copies touching (gap 0-2 s); the sampling Whisper falls back
to makes them come and go between runs, so no licence-free recording reproduces
them reliably. This checks the rule on segments shaped like the measured ones.
"""

from minutenest.transcribe import merge_repeated_segments


def seg(start: float, end: float, text: str) -> dict:
    return {"start": start, "end": end, "text": text}


def test_a_short_phrase_repeated_back_to_back_is_written_once():
    copies = [seg(10.0, 15.1, "Прикой."), seg(15.1, 16.1, "Прикой."), seg(16.1, 19.8, "Прикой."),
              seg(19.8, 20.8, "Прикой."), seg(20.8, 23.1, "Прикой."), seg(23.1, 26.0, "Вот это.")]
    assert merge_repeated_segments(copies) == [seg(10.0, 23.1, "Прикой."), seg(23.1, 26.0, "Вот это.")]


def test_copies_that_differ_only_in_case_and_punctuation_count_as_repeats():
    assert merge_repeated_segments([seg(0, 1, "Да."), seg(1, 2, "да"), seg(2, 3, "Да!")]) == [seg(0, 3, "Да.")]


def test_copies_up_to_two_seconds_apart_are_still_one_repeat():
    copies = [seg(0, 1, "Так."), seg(1, 2, "Так."), seg(2, 3, "Так."), seg(5, 6, "Так.")]
    assert merge_repeated_segments(copies) == [seg(0, 6, "Так.")]


def test_the_same_short_phrase_said_again_later_is_kept():
    said_twice = [seg(0, 1, "Да."), seg(9, 10, "Да.")]
    assert merge_repeated_segments(said_twice) == said_twice


def test_a_whole_sentence_said_twice_is_kept():
    sentence = "Давайте ещё раз проверим этот сценарий завтра."
    twice = [seg(0, 4, sentence), seg(4, 8, sentence)]
    assert merge_repeated_segments(twice) == twice


def test_different_neighbours_are_left_alone():
    talk = [seg(0, 2, "Привет."), seg(2, 4, "Привет, как дела?"), seg(4, 5, "Хорошо.")]
    assert merge_repeated_segments(talk) == talk
