"""Offline tests for safety.py: redaction, keyword rules and the gate. No network, no LLM."""
import pytest

from llm import Analysis
from safety import detect_language, gate, is_empty, redact, rule_hits

FAQ_IDS = {f"F{i:02d}" for i in range(1, 13)}


# --- redaction ----------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Mijn IBAN is NL91ABNA0417164300, graag",
    "Mijn IBAN is NL91 ABNA 0417 1643 00 graag",
    "iban nl91abna0417164300",
])
def test_valid_iban_is_masked(text):
    out = redact(text)
    assert "[IBAN removed]" in out and "0417" not in out


@pytest.mark.parametrize("text", ["kaart 4111 1111 1111 1111", "kaart 4111-1111-1111-1111", "4111111111111111"])
def test_valid_card_is_masked(text):
    assert "[card number removed]" in redact(text)


@pytest.mark.parametrize("text", [
    "Ordernummer 4111111111111112",       # 16 digits, fails Luhn
    "IBAN NL00ABNA0417164300",            # fails mod-97
    "Bel me op 0612345678",               # phone number, too short for a card
    "25 medewerkers voor €150 per maand",
])
def test_other_numbers_are_left_alone(text):
    assert redact(text) == text


@pytest.mark.parametrize("text, secret", [
    ("nieuwe kaart, geldig tot 09/28, CVC 123.", "123"),      # NL
    ("new card, expires 09/28, CVV: 456", "456"),             # EN
    ("neue Karte, gültig bis 09/28, Prüfnummer 789", "789"),  # DE
])
def test_card_security_code_and_expiry_are_masked(text, secret):
    out = redact(text)
    assert secret not in out and "09/28" not in out


# --- keyword rules: every topic in Dutch, English and German ------------------

def ticket(body, subject=""):
    return {"id": "T", "subject": subject, "body": body}


@pytest.mark.parametrize("flag, nl, en, de", [
    ("refund",
     "We waren in augustus dicht. Kunnen we die maand terugkrijgen?",  # E03
     "Please refund last month.",
     "Bitte erstatten Sie uns den Betrag, wir möchten eine Rückerstattung."),
    ("disputed_charge",
     "We zijn twee keer afgeschreven voor oktober.",
     "We were charged twice for October.",
     "Uns wurde der Oktober doppelt abgebucht."),
    ("cancellation",
     "Ik wil mijn abonnement opzeggen.",
     "I want to cancel my subscription.",
     "Ich möchte mein Abo kündigen."),
    ("contract_change",
     "Kunnen we het contract aanpassen naar 12 maanden?",
     "Can we change our contract to monthly?",
     "Können wir den Vertrag auf monatlich umstellen?"),
    ("personal_data_request",
     "Ik wil inzage in alle persoonsgegevens die jullie bewaren (AVG).",  # E09
     "Under GDPR, please send me all personal data you hold.",
     "Gemäß DSGVO bitte ich um Datenauskunft."),
    ("security_incident",
     "Iemand heeft ons account gehackt!",
     "I think our account was hacked, there was a suspicious login.",
     "Unser Konto wurde gehackt."),
    ("legal_threat",
     "Anders schakel ik mijn advocaat in.",
     "Otherwise I will take legal action.",
     "Sonst schalte ich meinen Anwalt ein."),
    ("threat_to_leave",
     "Als dit niet opgelost wordt, stappen we over naar een andere aanbieder.",
     "If this isn't fixed we are switching to another provider.",
     "Sonst wechseln wir zu einem anderen Anbieter."),
])
def test_sensitive_topics_are_caught_in_all_languages(flag, nl, en, de):
    for body in (nl, en, de):
        assert flag in rule_hits(ticket(body)), body


@pytest.mark.parametrize("body", [
    "Ons btw-nummer is veranderd. Kunnen jullie het nieuwe op onze facturen zetten?",  # E02
    "Hoe ruil ik een dienst met een collega?",                                        # E04
    "Wat kost Plus voor 25 medewerkers?",                                              # E07
    "Hoe stel ik de contracturen van een medewerker in?",  # 'contracturen' is not 'contract'
    "De knop staat in de rechter kolom.",                  # 'rechter' = right-hand, not judge
    "The app crashes on my iPhone 8.",
    "How do I add a new employee?",
    "Wie tausche ich eine Schicht mit einem Kollegen?",
    "Unser Tagesgericht wird nicht angezeigt.",            # 'Gericht' = dish, not court
])
def test_normal_questions_are_not_caught(body):
    assert rule_hits(ticket(body)) == []


@pytest.mark.parametrize("subject", ["", "Re:", "RE: Fwd:", "AW:", "Antw:"])
def test_reply_prefix_without_body_is_empty(subject):
    assert is_empty(ticket("", subject))


def test_subject_without_body_is_not_empty():
    assert not is_empty(ticket("", "Wachtwoord vergeten"))


# --- language detection -------------------------------------------------------

@pytest.mark.parametrize("text, lang", [
    ("Wat kost het Plus-abonnement voor 25 medewerkers per maand?", "nl"),
    ("The app crashes immediately on my iPhone 8 with iOS 15.", "en"),
    ("Ich habe mein Passwort vergessen, bitte helfen Sie mir.", "de"),
])
def test_language_is_detected(text, lang):
    assert detect_language(text) == lang


def test_mixed_language_is_not_guessed():
    # An English ticket quoting a German button: too mixed to be sure, so the LLM's answer stands.
    assert detect_language("Typo. In the German version the button says 'Schicht tauschenn'. Small thing.") is None


# --- gate ---------------------------------------------------------------------

REPLY = dict(
    language="nl", english_summary="How to swap a shift.", category="question", priority="P4",
    action="reply", reason="F03 answers it.", faq_ids=["F03"], sensitive=[], confidence=0.9,
    draft_reply="Je kunt een dienst ruilen via de app: open de dienst en kies Ruilen met een collega.",
    handoff_note="",
)
SWAP = ticket("Hoe ruil ik een dienst met een collega in de app?")


def test_clean_reply_passes():
    d = gate(SWAP, Analysis(**REPLY), [], FAQ_IDS)
    assert (d.action, d.failed_checks) == ("reply", [])


@pytest.mark.parametrize("change, hits, expected_check", [
    ({}, ["refund"], "keyword rule"),
    ({"sensitive": ["cancellation"]}, [], "LLM flagged"),
    ({"faq_ids": []}, [], "cites no FAQ"),
    ({"faq_ids": ["F99"]}, [], "unknown FAQ id"),
    ({"confidence": 0.5}, [], "confidence"),
    ({"draft_reply": "You can swap a shift in the app: open the shift and choose Swap."}, [], "draft is not in nl"),
    ({"draft_reply": "Uw IBAN NL91ABNA0417164300 is genoteerd, wij betalen het bedrag uit aan u."}, [], "payment details"),
])
def test_unsafe_reply_becomes_handoff(change, hits, expected_check):
    d = gate(SWAP, Analysis(**{**REPLY, **change}), hits, FAQ_IDS)
    assert d.action == "handoff"
    assert any(expected_check in c for c in d.failed_checks)
    assert d.handoff_note and not d.draft_reply


def test_sensitive_topic_raises_priority():
    d = gate(SWAP, Analysis(**REPLY), ["security_incident"], FAQ_IDS)
    assert d.priority == "P1"
    d = gate(SWAP, Analysis(**REPLY), ["refund"], FAQ_IDS)
    assert d.priority == "P2"


def test_unsure_close_becomes_handoff():
    spam = {**REPLY, "action": "close", "category": "other", "draft_reply": "", "faq_ids": [], "confidence": 0.6}
    assert gate(SWAP, Analysis(**spam), [], FAQ_IDS).action == "handoff"


def test_handoff_note_is_redacted():
    h = {**REPLY, "action": "handoff", "draft_reply": "", "handoff_note": "Card 4111 1111 1111 1111 was charged."}
    assert "4111" not in gate(SWAP, Analysis(**h), [], FAQ_IDS).handoff_note
