"""D. How the LLM stage's share of the bar is counted (TEST_PLAN.md D31).

Rule-level: the progress the card shows, fed the way a streamed answer feeds it.
"""

from minutenest.polish import Progress


def test_the_bar_moves_on_as_soon_as_the_model_starts_writing_after_a_long_think():
    """After a long think the bar used to stand still until the document alone
    caught up with the thinking's share (about a third of the document)."""
    shown: list[float] = []
    slot = Progress(1, lambda fraction, _message: shown.append(fraction)).start("writing the document", 1_500)
    slot.streamed(0, 20_000)          # a long think: 20 000 characters of reasoning
    after_thinking = shown[-1]
    slot.streamed(150, 20_000)        # the first tenth of the document arrives
    assert shown[-1] > after_thinking
