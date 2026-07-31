from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


PHRASE_SYNONYMS = {
    'guaranteed minimum': 'minimum',
    'front end gross': 'frontend',
    'front gross': 'frontend',
    'front end': 'frontend',
    'back end': 'backend',
    'f and i': 'backend',
    'finance commission': 'backend commission',
    'finance': 'backend',
    'front': 'frontend',
    'back': 'backend',
    'frontend': 'frontend',
    'backend': 'backend',
    'floor': 'minimum',
    'mini': 'minimum',
    'min': 'minimum',
    'cap': 'maximum',
    'max': 'maximum',
    'commision': 'commission',
    'payout': 'commission',
    'earnings': 'commission',
    'cars': 'units',
    'car': 'unit',
    'vehicles': 'units',
    'vehicle': 'unit',
    'deals': 'units',
    'deal': 'unit',
    'sales': 'units',
    'sale': 'unit',
}

ACTION_SYNONYMS = {
    'add': ('add', 'create', 'introduce'),
    'change': (
        'change', 'set', 'make', 'use', 'update', 'should be', 'needs to be',
        'pay at least',
    ),
    'remove': ('remove', 'drop', 'delete', 'without', 'no longer require'),
    'replace': ('replace', 'swap'),
    'increase': ('increase', 'raise', 'bump'),
    'decrease': ('decrease', 'lower', 'reduce'),
    'enable': ('enable', 'turn on'),
    'disable': ('disable', 'turn off'),
    'rename': ('rename',),
    'duplicate': ('duplicate', 'copy'),
}

SMALL_NUMBERS = {
    'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
    'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
    'nineteen': 19, 'twenty': 20, 'thirty': 30, 'forty': 40,
    'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90,
}
SCALES = {'hundred': 100, 'thousand': 1000}
NUMBER_WORDS = frozenset((*SMALL_NUMBERS, *SCALES, 'and'))


@dataclass(frozen=True)
class NumericMention:
    value: Decimal
    start: int
    end: int
    is_currency: bool
    is_percentage: bool
    is_unit_threshold: bool


def words_to_numbers(text: str) -> str:
    tokens = text.split()
    output = []
    index = 0
    while index < len(tokens):
        clean = tokens[index].strip('.,!?;:()')
        if clean not in NUMBER_WORDS or clean == 'and':
            output.append(tokens[index])
            index += 1
            continue
        end = index
        sequence = []
        while end < len(tokens):
            candidate = tokens[end].strip('.,!?;:()')
            if candidate not in NUMBER_WORDS:
                break
            sequence.append(candidate)
            end += 1
        meaningful = [item for item in sequence if item != 'and']
        if not meaningful:
            output.append(tokens[index])
            index += 1
            continue
        total = 0
        current = 0
        for item in meaningful:
            if item in SMALL_NUMBERS:
                current += SMALL_NUMBERS[item]
            elif item == 'hundred':
                current = max(current, 1) * 100
            elif item == 'thousand':
                total += max(current, 1) * 1000
                current = 0
        output.append(str(total + current))
        index = end
    return ' '.join(output)


def normalize_text(source_text: str) -> str:
    text = unicodedata.normalize('NFKC', source_text or '').casefold()
    text = text.replace('f&i', 'f and i')
    text = re.sub(r'[\u2010-\u2015_-]+', ' ', text)
    text = re.sub(r'(?<=\d),(?=\d)', '', text)
    text = words_to_numbers(text)
    for phrase in sorted(PHRASE_SYNONYMS, key=len, reverse=True):
        replacement = PHRASE_SYNONYMS[phrase]
        text = re.sub(rf'\b{re.escape(phrase)}\b', replacement, text)
    text = re.sub(r'[^\w\s.$%]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def numeric_mentions(normalized_text: str) -> tuple[NumericMention, ...]:
    pattern = re.compile(
        r'(?P<currency>\$)\s*'
        r'(?P<currency_number>\d+(?:\.\d+)?)'
        r'|(?P<number>\d+(?:\.\d+)?)\s*'
        r'(?P<suffix>%|percent|dollars?|bucks?)?'
    )
    mentions = []
    for match in pattern.finditer(normalized_text):
        raw = match.group('currency_number') or match.group('number')
        try:
            value = Decimal(raw)
        except InvalidOperation:
            continue
        suffix = match.group('suffix') or ''
        following = normalized_text[match.end():match.end() + 18]
        mentions.append(NumericMention(
            value=value,
            start=match.start(),
            end=match.end(),
            is_currency=bool(match.group('currency')) or suffix in {
                'dollar', 'dollars', 'buck', 'bucks',
            },
            is_percentage=suffix in {'%', 'percent'},
            is_unit_threshold=bool(re.match(r'\s*units?\b', following)),
        ))
    return tuple(mentions)


def detect_action(normalized_text: str) -> str | None:
    for action, phrases in ACTION_SYNONYMS.items():
        if any(re.search(rf'\b{re.escape(item)}\b', normalized_text) for item in phrases):
            return action
    return None
