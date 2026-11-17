"""Offline tests for llm.py. Network retries are the Anthropic SDK's job, so they are not tested here."""
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from llm import Analysis, Analyzer, ClaudeClient, InvalidAnswer, LLMError, check_analysis

TICKET = {"id": "T001", "subject": "Wachtwoord", "body": "Ik ben mijn wachtwoord vergeten."}
GOOD = dict(
    language="nl", english_summary="Forgot password.", category="question", priority="P4",
    action="reply", reason="FAQ F01 answers it.", faq_ids=["F01"], sensitive=[],
    confidence=0.9, draft_reply="Ga naar de loginpagina...", handoff_note="",
)
GOOD_ANSWER = (Analysis(**GOOD), "end_turn", 100, 20)
NO_DRAFT = (Analysis(**{**GOOD, "draft_reply": ""}), "end_turn", 100, 20)
REFUSAL = (None, "refusal", 100, 0)


class FakeClaude:
    """Stands in for ClaudeClient: returns the given answers one by one and counts the calls."""

    model = "fake-model"

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    async def ask(self, system, user):
        self.calls += 1
        return self.answers.pop(0)


# --- Analyzer -----------------------------------------------------------------

async def test_good_answer_is_returned_and_tokens_counted():
    analyzer = Analyzer(FakeClaude(GOOD_ANSWER), faq_text="F01 ...")
    assert (await analyzer.analyze(TICKET)).action == "reply"
    assert (analyzer.tokens_in, analyzer.tokens_out) == (100, 20)


async def test_broken_answer_is_asked_again():
    claude = FakeClaude(NO_DRAFT, GOOD_ANSWER)
    await Analyzer(claude, faq_text="F01 ...").analyze(TICKET)
    assert claude.calls == 2


async def test_refusal_is_asked_again():
    claude = FakeClaude(REFUSAL, GOOD_ANSWER)
    await Analyzer(claude, faq_text="F01 ...").analyze(TICKET)
    assert claude.calls == 2


async def test_gives_up_after_three_broken_answers():
    claude = FakeClaude(REFUSAL, REFUSAL, REFUSAL)
    with pytest.raises(LLMError, match="after 3 attempts"):
        await Analyzer(claude, faq_text="F01 ...").analyze(TICKET)
    assert claude.calls == 3


@pytest.mark.parametrize("change", [
    {"draft_reply": "  "},                     # reply without a draft
    {"action": "handoff", "draft_reply": ""},  # handoff without a note
    {"confidence": 1.5},                       # confidence out of range
])
def test_broken_answers_are_rejected(change):
    with pytest.raises(InvalidAnswer):
        check_analysis(Analysis(**{**GOOD, **change}))


# --- ClaudeClient -------------------------------------------------------------

async def test_api_error_becomes_llm_error():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    async def parse(**_):  # what the SDK raises once its own retries are used up
        raise anthropic.APIConnectionError(request=request)

    claude = ClaudeClient(api_key="test", model="test-model")
    claude._client = SimpleNamespace(messages=SimpleNamespace(parse=parse))
    with pytest.raises(LLMError, match="APIConnectionError") as caught:
        await claude.ask("system", "user")
    assert not isinstance(caught.value, InvalidAnswer)  # so the Analyzer won't ask again
