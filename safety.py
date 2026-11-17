"""Safety layer: plain code, no LLM. It can only make a decision safer, never riskier.

  redact()      masks IBANs and card numbers (checksum-validated) before text reaches the LLM or a log
  rule_hits()   keyword rules (NL/EN/DE) for topics the client's rules always send to a human
  gate()        turns the LLM's analysis into the final decision; a reply survives only if every check passes
"""
import re
from dataclasses import dataclass, field

from lingua import Language, LanguageDetectorBuilder

from llm import Analysis

MIN_REPLY_CONFIDENCE = 0.75
MIN_CLOSE_CONFIDENCE = 0.9

# --- redaction ----------------------------------------------------------------

# Country code, 2 check digits, then 11-30 letters/digits, optionally in groups of 4 separated by spaces.
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b", re.IGNORECASE)
# 13-19 digits, optionally separated by single spaces or dashes.
CARD_RE = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
# A card's security code or expiry date, recognised by its label in NL / EN / DE. The label stays, the value goes.
CVC_RE = re.compile(
    r"\b(cvc2?|cvv2?|csc|security code|beveiligingscode|kaartcode|sicherheitscode|kartenprüfnummer|prüfnummer)"
    r"(\W{0,3})\d{3,4}\b", re.IGNORECASE)
EXPIRY_RE = re.compile(
    r"\b(geldig tot|vervaldatum|verloopt|valid (?:until|thru|through)|expiry(?: date)?|expires|exp\.?"
    r"|gültig bis|ablaufdatum|läuft ab)(\W{0,3})\d{1,2}\s*/\s*\d{2,4}\b", re.IGNORECASE)


def _valid_iban(candidate: str) -> bool:
    s = candidate.replace(" ", "").upper()
    if not 15 <= len(s) <= 34:
        return False
    digits = "".join(str(int(c, 36)) for c in s[4:] + s[:4])  # A=10 ... Z=35
    return int(digits) % 97 == 1


def _valid_card(candidate: str) -> bool:
    """Luhn checksum: catches real card numbers, skips random long numbers like order ids."""
    digits = [int(c) for c in candidate if c.isdigit()]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def redact(text: str) -> str:
    text = IBAN_RE.sub(lambda m: "[IBAN removed]" if _valid_iban(m.group()) else m.group(), text)
    text = CARD_RE.sub(lambda m: "[card number removed]" if _valid_card(m.group()) else m.group(), text)
    text = CVC_RE.sub(r"\1\2[removed]", text)
    return EXPIRY_RE.sub(r"\1\2[removed]", text)


def redact_ticket(ticket: dict) -> dict:
    """A copy of the ticket with payment details masked in the fields the LLM sees."""
    return {**ticket, "subject": redact(ticket.get("subject") or ""), "body": redact(ticket.get("body") or "")}


# --- keyword rules: a backstop, NOT the main detector -------------------------
# The main detector is the LLM's `sensitive` flags, which understand meaning and catch phrasings no list can.
# On top of that, a reply is only allowed when gate() finds positive reasons for it, so a missed keyword
# does not by itself let an unsafe reply through.
# These keywords exist so the most obvious, high-stakes words still force a handoff if the model slips.
# They are the everyday vocabulary of each client rule (refund, AVG, opzeggen, advocaat, ...) in NL / EN / DE,
# written from the rule list, not from the tickets we test on. They will never be complete, by design.
# Patterns are matched on lowercased text; \b keeps e.g. "contract" from matching "contracturen".

RULES: dict[str, dict[str, list[str]]] = {
    "refund": {
        "nl": [r"terugbetal", r"terugstort", r"terugkrijgen", r"terug ?krijgen", r"geld terug", r"restitutie"],
        "en": [r"\brefund", r"money back", r"reimburse", r"pay (?:us|me) back"],
        "de": [r"rückerstatt", r"erstattung", r"geld zurück", r"zurückzahl", r"zurückbekommen"],
    },
    "disputed_charge": {  # NL "afgeschreven" / DE "abgebucht" = debited
        "nl": [r"dubbel (?:afgeschreven|betaald|gefactureerd)", r"twee ?keer (?:afgeschreven|betaald|gefactureerd)",
               r"onterecht (?:afgeschreven|gefactureerd|betaald|in rekening)", r"terugboek", r"storno"],
        "en": [r"chargeback", r"charged twice", r"double[- ]charged?", r"wrongly charged", r"dispute (?:the|a|this) charge",
               r"unauthori[sz]ed (?:charge|payment)"],
        "de": [r"doppelt (?:abgebucht|berechnet|bezahlt)", r"zweimal (?:abgebucht|berechnet|bezahlt)",
               r"falsch (?:abgebucht|berechnet)", r"rückbuchung", r"lastschrift zurück"],
    },
    "cancellation": {
        "nl": [r"opzeg", r"abonnement (?:stoppen|beëindigen|annuleren)", r"account (?:opheffen|verwijderen)"],
        "en": [r"cancel (?:my|our|the) (?:subscription|account|plan|contract)", r"\bterminate", r"close (?:my|our) account"],
        "de": [r"kündig", r"abo(?:nnement)? (?:beenden|stornieren)", r"konto (?:löschen|schließen)"],
    },
    "contract_change": {
        "nl": [r"\bcontract\b", r"\bdowngrade", r"overeenkomst (?:wijzigen|aanpassen)"],
        "en": [r"\bcontract\b", r"\bdowngrade", r"change (?:my|our) (?:plan|contract)"],
        "de": [r"\bvertrag\b", r"vertragsänderung", r"tarif (?:ändern|wechseln)"],
    },
    "personal_data_request": {  # AVG = Dutch GDPR, DSGVO = German GDPR
        "nl": [r"\bavg\b", r"inzageverzoek", r"persoonsgegevens", r"verwijder (?:al )?(?:mijn|onze) gegevens"],
        "en": [r"\bgdpr\b", r"personal data", r"delete (?:my|our|all) data", r"right to be forgotten",
               r"subject access request"],
        "de": [r"\bdsgvo\b", r"datenauskunft", r"personenbezogene daten", r"meine daten löschen", r"auskunftsersuchen"],
    },
    "security_incident": {
        "nl": [r"gehackt", r"datalek", r"verdachte (?:login|inlog|activiteit)", r"onbevoegd",
               r"account (?:is )?overgenomen"],
        "en": [r"\bhacked", r"phishing", r"data ?breach", r"suspicious (?:login|activity)",
               r"unauthori[sz]ed (?:access|login)", r"account (?:was )?(?:compromised|taken over)"],
        "de": [r"gehackt", r"datenleck", r"verdächtige (?:anmeldung|aktivität)", r"unbefugt", r"konto übernommen"],
    },
    "legal_threat": {
        "nl": [r"advocaat", r"rechtszaak", r"juridische stappen", r"aansprakelijk", r"ingebrekestelling", r"naar de rechter"],
        "en": [r"\blawyer", r"attorney", r"legal action", r"\bsue\b", r"lawsuit", r"\bliable\b"],
        "de": [r"anwalt", r"rechtliche schritte", r"\bklage\b", r"vor gericht", r"haftbar"],
    },
    "threat_to_leave": {
        "nl": [r"overstappen naar", r"andere aanbieder", r"\bconcurrent\b", r"stoppen met roosterly", r"we vertrekken"],
        "en": [r"switch(?:ing)? to (?:another|a different|a competitor)", r"leave roosterly", r"competitor",
               r"another provider"],
        "de": [r"wechseln zu", r"anderen anbieter", r"konkurrenz", r"roosterly verlassen"],
    },
}
# All three languages are checked on every ticket: customers mix them (an English ticket quoting a German button).
_COMPILED = {flag: [re.compile(p) for ps in by_lang.values() for p in ps] for flag, by_lang in RULES.items()}


def rule_hits(ticket: dict) -> list[str]:
    text = f"{ticket.get('subject') or ''}\n{ticket.get('body') or ''}".lower()
    return [flag for flag, patterns in _COMPILED.items() if any(p.search(text) for p in patterns)]


# Reply/forward prefixes in NL / EN / DE: a subject like "Re:" with no body is still an empty ticket.
_PREFIXES = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|wg|antw|doorst)\s*:\s*)*", re.IGNORECASE)


def is_empty(ticket: dict) -> bool:
    subject = _PREFIXES.sub("", ticket.get("subject") or "")
    return not re.search(r"\w", subject) and not (ticket.get("body") or "").strip()


# --- language check (offline, no LLM) -----------------------------------------

_DETECTOR = LanguageDetectorBuilder.from_languages(Language.DUTCH, Language.ENGLISH, Language.GERMAN).build()
# Clean nl/en/de text scores 0.96+; mixed text (an English ticket quoting a German button) scores ~0.77.
MIN_LANGUAGE_CONFIDENCE = 0.9


def detect_language(text: str) -> str | None:
    """'nl', 'en', 'de', or None when the text is too short or too mixed to be sure."""
    if len(text.strip()) < 20:
        return None
    best = _DETECTOR.compute_language_confidence_values(text)[0]
    return best.language.iso_code_639_1.name.lower() if best.value >= MIN_LANGUAGE_CONFIDENCE else None


def draft_in_language(draft: str, lang: str) -> bool:
    """False when another language clearly dominates the draft (the customer's language scores < 0.5).
    Looser than detect_language on purpose: short clean drafts score only ~0.8, a wrong-language draft ~0.15."""
    if len(draft.strip()) < 20:
        return True
    scores = {c.language.iso_code_639_1.name.lower(): c.value
              for c in _DETECTOR.compute_language_confidence_values(draft)}
    return scores.get(lang, 0) >= 0.5


# --- the gate -----------------------------------------------------------------

@dataclass
class Decision:
    action: str
    language: str
    category: str
    priority: str
    reason: str
    confidence: float
    draft_reply: str = ""
    handoff_note: str = ""
    failed_checks: list[str] = field(default_factory=list)  # why the gate overruled the LLM (empty if it didn't)

    def body(self) -> dict:
        """The JSON body for POST /tickets/{id}/{action}."""
        b = {"language": self.language, "category": self.category, "priority": self.priority,
             "reason": self.reason, "confidence": round(self.confidence, 2)}
        if self.action == "reply":
            b["draft_reply"] = self.draft_reply
        elif self.action == "handoff":
            b["handoff_note"] = self.handoff_note
        return b


def empty_ticket_decision() -> Decision:
    return Decision("close", "nl", "other", "P4", "Empty ticket: nothing to answer.", 1.0)


PRIORITY_FLOOR = {"security_incident": "P1", "legal_threat": "P1"}  # every other sensitive topic: at least P2


def _raise_priority(priority: str, flags: set[str]) -> str:
    """Rules can only make a ticket more urgent. P1 < P2 < P3 < P4 as strings, so min() is the most urgent."""
    floors = [PRIORITY_FLOOR.get(f, "P2") for f in flags]
    return min([priority, *floors])


def gate(ticket: dict, a: Analysis, hits: list[str], faq_ids: set[str]) -> Decision:
    """Final decision. Plain code decides whether the LLM's reply or close is allowed; otherwise it is a handoff."""
    flags = set(hits) | set(a.sensitive)
    failed: list[str] = []

    if hits:
        failed.append(f"keyword rule: {', '.join(hits)}")
    if a.sensitive:
        failed.append(f"LLM flagged: {', '.join(a.sensitive)}")
    ticket_lang = detect_language(f"{ticket.get('subject') or ''}. {ticket.get('body') or ''}")
    if ticket_lang and ticket_lang != a.language:
        failed.append(f"ticket looks {ticket_lang}, LLM said {a.language}")

    if a.action == "reply":
        if not a.faq_ids:
            failed.append("reply cites no FAQ entry")
        elif unknown := [f for f in a.faq_ids if f not in faq_ids]:
            failed.append(f"unknown FAQ id {', '.join(unknown)}")
        if a.confidence < MIN_REPLY_CONFIDENCE:
            failed.append(f"confidence {a.confidence:.2f} < {MIN_REPLY_CONFIDENCE}")
        if not draft_in_language(a.draft_reply, ticket_lang or a.language):
            failed.append(f"draft is not in {ticket_lang or a.language}, the customer's language")
        if redact(a.draft_reply) != a.draft_reply:
            failed.append("draft contains payment details")
    elif a.action == "close" and a.confidence < MIN_CLOSE_CONFIDENCE:
        failed.append(f"close with confidence {a.confidence:.2f} < {MIN_CLOSE_CONFIDENCE}")

    language = ticket_lang or a.language
    priority = _raise_priority(a.priority, flags)
    if a.action == "handoff" or not failed:
        return Decision(a.action, language, a.category, priority, redact(a.reason), a.confidence,
                        a.draft_reply if a.action == "reply" else "",  # a draft with payment details never gets here
                        redact(a.handoff_note) if a.action == "handoff" else "", failed)

    # The LLM wanted to reply or close, but a check failed: hand off instead, and say why.
    note = redact(f"{a.english_summary} Escalated by the safety checks: {'; '.join(failed)}.")
    reason = redact(f"{a.reason} Handed off: {'; '.join(failed)}.")
    return Decision("handoff", language, a.category, priority, reason, a.confidence, "", note, failed)
