"""Greek text helpers shared by the correction stages.

Three things the correction stages all need and must agree on: how to compare
words written in 1977 capitals against a modern lexicon, which letter pairs the
OCR actually confuses, and when two spellings are the same word.
"""
from __future__ import annotations

import re
import unicodedata

# Uppercase Greek letters the OCR mixes up, grouped by the strokes they share.
# Hand-seeded; learn_confusions() replaces this with pairs observed in the data.
CONFUSIONS: dict[str, str] = {
    "Α": "ΛΔ", "Λ": "ΑΔ", "Δ": "ΑΛ",
    "Ο": "ΘΩΣΕΟ", "Θ": "ΟΩΦ", "Ω": "ΟΘ", "Φ": "ΘΟ",
    "Ε": "ΣΞΖΟ", "Σ": "ΕΞΖΟ", "Ξ": "ΖΣΕ", "Ζ": "ΞΣΕ",
    "Η": "ΠΝΜΙ", "Π": "ΗΝΓΤ", "Ν": "ΗΠΜΥ", "Μ": "ΝΗ",
    "Ι": "ΓΤΡΗ", "Γ": "ΙΤΡΠ", "Τ": "ΙΓΡ", "Ρ": "ΒΙΓ", "Β": "ΡΘΕ",
    "Κ": "ΧΙ", "Χ": "ΚΥ", "Υ": "ΧΨΝ", "Ψ": "Υ",
}

GREEK_WORD = re.compile(r"[Α-Ωα-ωΆ-ώϊϋΐΰ]+")

# Greek spellings that sound identical. Nothing here is an OCR error: /i/ is
# written six ways and /o/ two, so ΣΕΒΑΣΜΙΩΤΑΤΟΣ and ΣΕΒΑΣΜΙΟΤΑΤΟΣ are one word
# in two orthographies. Folding these keeps period spelling from being "fixed".
_PHONETIC = [
    ("ΑΙ", "Ε"),
    ("ΕΙ", "Ι"), ("ΟΙ", "Ι"), ("ΥΙ", "Ι"),
    ("Η", "Ι"), ("Υ", "Ι"),
    ("Ω", "Ο"),
    ("ΜΠ", "Β"), ("ΝΤ", "Δ"), ("ΓΚ", "Γ"),
    ("ΑΥ", "ΑΦ"), ("ΕΥ", "ΕΦ"),
]


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def fold(word: str) -> str:
    """Accent-stripped uppercase, so headline capitals match a modern lexicon."""
    out = strip_accents(word).upper()
    return out.replace("Ϊ", "Ι").replace("Ϋ", "Υ")


def phonetic_fold(word: str) -> str:
    """A spelling-insensitive key: equal keys mean the words sound the same."""
    out = fold(word)
    for src, dst in _PHONETIC:
        out = out.replace(src, dst)
    # Final sigma and doubled consonants do not change the sound.
    out = out.replace("Σ", "Σ")
    return re.sub(r"(.)\1+", r"\1", out)


def sounds_same(a: str, b: str) -> bool:
    """True when two spellings are the same word, e.g. ω/ο period variants."""
    return phonetic_fold(a) == phonetic_fold(b)


_ARCHAIC_ENDINGS = (
    ("ΟΝ", "ΟΥ"), ("ΟΝ", "ΟΣ"), ("ΗΝ", "ΗΣ"), ("ΑΝ", "ΑΣ"),
    ("ΕΩΣ", "ΗΣ"), ("ΟΙΣ", "ΟΥΣ"),
)


def is_morphological_variant(a: str, b: str) -> bool:
    """True when two forms differ only by case ending, not by a misread letter.

    The paper writes τον πρόεδρον and τον Γεώργιον, so ΠΡΟΕΔΡΟΝ beside ΠΡΟΕΔΡΟΥ
    is period grammar rather than OCR damage. Correcting it would modernise the
    text, which is the one thing a transcription must not do.
    """
    for first, second in _ARCHAIC_ENDINGS:
        for x, y in ((a, b), (b, a)):
            if x.endswith(first) and y.endswith(second) and x[: -len(first)] == y[: -len(second)]:
                return True
    return False


def looks_like_abbreviation(token: str, text_forms: set[str] | None = None) -> bool:
    """Short all-caps tokens such as ΑΘΑΝ. or ΑΡΙΘ. that a lexicon will reject.

    They are truncations, so no dictionary contains them and every nearby real
    word looks like a correction.
    """
    if len(token) > 5:
        return False
    if text_forms and f"{token}." in text_forms:
        return True
    return len(token) <= 4


def confusion_variants(token: str, confusions: dict[str, str] | None = None) -> set[str]:
    """Every word one confusable-letter substitution away from this one."""
    table = confusions if confusions is not None else CONFUSIONS
    out = set()
    for i, ch in enumerate(token):
        for alt in table.get(ch, ""):
            if alt != ch:
                out.add(token[:i] + alt + token[i + 1:])
    return out


def confusion_distance(a: str, b: str, confusions: dict[str, str] | None = None) -> float:
    """Edit distance charging less for substitutions the OCR is known to make.

    A confusable substitution costs 0.5 and any other edit 1.0, so ΑΟΥΤΡΑΚΙΟΥ
    sits nearer ΛΟΥΤΡΑΚΙΟΥ than an unrelated word of the same length does.
    """
    table = confusions if confusions is not None else CONFUSIONS
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [float(i)]
        for j, cb in enumerate(b, start=1):
            if ca == cb:
                cost = 0.0
            elif cb in table.get(ca, ""):
                cost = 0.5
            else:
                cost = 1.0
            current.append(min(previous[j] + 1.0, current[j - 1] + 1.0, previous[j - 1] + cost))
        previous = current
    return previous[-1]


def learn_confusions(pairs: list[tuple[str, str]]) -> dict[str, list[tuple[str, int]]]:
    """Count the substitutions seen in confirmed wrong/right pairs.

    Replaces guesswork with the errors this scan actually makes, and shows where
    the OCR is systematically weak.
    """
    counts: dict[str, dict[str, int]] = {}
    for wrong, right in pairs:
        if len(wrong) != len(right):
            continue
        for cw, cr in zip(wrong, right):
            if cw != cr:
                counts.setdefault(cw, {}).setdefault(cr, 0)
                counts[cw][cr] += 1
    return {
        src: sorted(dsts.items(), key=lambda kv: -kv[1])
        for src, dsts in sorted(counts.items())
    }
