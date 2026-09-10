#!/usr/bin/env python3
"""Generate Mouchen's compact offline IME lexicon from MIT-licensed data.

Development-only dependencies:
  python -m pip install jieba==0.42.1 pypinyin==0.55.0

The generated TSV is checked into the privateAlpha assets, so Android builds do
not need Python or a network connection.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from pypinyin import Style, lazy_pinyin, pinyin


CJK_WORD = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff]+$")
VALID_PINYIN = re.compile(r"^[a-zv]+$")
SECONDARY_READING_FREQUENCY_FACTOR = 0.05
SECONDARY_READING_FREQUENCY_CAP = 2_500
# pypinyin exposes pronunciations but not reading-level corpus frequency. Keep
# a small, explicit set of everyday secondary readings reachable in the nine
# visible candidates without letting every rare heteronym inherit full weight.
COMMON_SECONDARY_READING_FLOORS = {
    ("乐", "yue"): 2_500,
    ("角", "jue"): 2_500,
    ("觉", "jiao"): 2_500,
}

# Product and conversational phrases that should remain available even if their
# source-corpus frequency is below the global cutoff.
CURATED_FREQUENCIES = {
    "你好": 90_000_000,
    "谢谢": 88_000_000,
    "好的": 87_000_000,
    "可以": 86_000_000,
    "我们": 85_000_000,
    "你们": 84_000_000,
    "他们": 83_000_000,
    "今天": 82_000_000,
    "明天": 81_000_000,
    "现在": 80_000_000,
    "时间": 79_000_000,
    "问题": 78_000_000,
    "解决": 77_000_000,
    "方案": 76_000_000,
    "需要": 75_000_000,
    "已经": 74_000_000,
    "没有": 73_000_000,
    "继续": 72_000_000,
    "完成": 71_000_000,
    "确认": 70_000_000,
    "收到": 69_000_000,
    "马上": 68_000_000,
    "稍等": 67_000_000,
    "没问题": 66_000_000,
    "辛苦了": 65_000_000,
    "麻烦你": 64_000_000,
    "怎么办": 63_000_000,
    "为什么": 62_000_000,
    "怎么做": 61_000_000,
    "有没有": 60_000_000,
    "我觉得": 59_000_000,
    "我认为": 58_000_000,
    "我需要": 57_000_000,
    "我可以": 56_000_000,
    "你可以": 55_000_000,
    "我们可以": 54_000_000,
    "下一步": 53_000_000,
    "第一步": 52_000_000,
    "注意安全": 51_000_000,
    "AI替身": 100_000_000,
    "建言": 99_000_000,
    "献策": 98_000_000,
    "谏言": 97_000_000,
    "目标": 96_000_000,
    "承诺": 95_000_000,
    "复盘": 94_000_000,
    "风险": 93_000_000,
    "机会": 92_000_000,
    "提醒": 91_000_000,
    "全拼": 90_500_000,
    "双拼": 90_400_000,
    "自然码": 90_300_000,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jieba-dict", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-entries", type=int, default=50_000)
    parser.add_argument("--max-single-characters", type=int, default=7_000)
    return parser.parse_args()


def read_frequencies(path: Path) -> dict[str, int]:
    frequencies: dict[str, int] = dict(CURATED_FREQUENCIES)
    with path.open("r", encoding="utf-8") as source:
        for raw_line in source:
            fields = raw_line.rstrip("\n").split(" ")
            if len(fields) < 2:
                continue
            word = fields[0]
            if not 1 <= len(word) <= 6 or not CJK_WORD.fullmatch(word):
                continue
            try:
                frequency = int(fields[1])
            except ValueError:
                continue
            frequencies[word] = max(frequencies.get(word, 0), frequency)
    return frequencies


def select_words(
    frequencies: dict[str, int],
    max_entries: int,
    max_single_characters: int,
) -> list[tuple[str, int]]:
    singles = sorted(
        ((word, frequency) for word, frequency in frequencies.items() if len(word) == 1),
        key=lambda item: (-item[1], item[0]),
    )[:max_single_characters]
    remaining = max(0, max_entries - len(singles))
    phrases = sorted(
        ((word, frequency) for word, frequency in frequencies.items() if len(word) > 1),
        key=lambda item: (-item[1], item[0]),
    )[:remaining]
    return singles + phrases


def spellings_for(word: str) -> list[tuple[str, bool]]:
    if len(word) == 1:
        primary_values = lazy_pinyin(
            word,
            style=Style.NORMAL,
            strict=False,
            neutral_tone_with_five=False,
            errors="ignore",
        )
        primary = (
            primary_values[0].lower().replace("u:", "v").replace("ü", "v")
            if len(primary_values) == 1
            else ""
        )
        groups = pinyin(
            word,
            style=Style.NORMAL,
            heteronym=True,
            strict=False,
            neutral_tone_with_five=False,
            errors="ignore",
        )
        raw_spellings = groups[0] if len(groups) == 1 else []
        normalized = [
            value.lower().replace("u:", "v").replace("ü", "v")
            for value in raw_spellings
        ]
        spellings = sorted({value for value in normalized if VALID_PINYIN.fullmatch(value)})
        return [(value, value == primary) for value in spellings]

    syllables = lazy_pinyin(
        word,
        style=Style.NORMAL,
        strict=False,
        neutral_tone_with_five=False,
        errors="ignore",
    )
    if len(syllables) != len(word):
        return []
    normalized = [
        syllable.lower().replace("u:", "v").replace("ü", "v")
        for syllable in syllables
    ]
    if any(not VALID_PINYIN.fullmatch(syllable) for syllable in normalized):
        return []
    return [("'".join(normalized), True)]


def main() -> None:
    args = parse_args()
    if args.max_entries < 1:
        raise SystemExit("--max-entries must be positive")
    frequencies = read_frequencies(args.jieba_dict)
    entries: list[tuple[str, str, int]] = []
    for word, frequency in select_words(
        frequencies,
        args.max_entries,
        args.max_single_characters,
    ):
        for spelling, is_primary in spellings_for(word):
            reading_frequency = frequency if is_primary else min(
                SECONDARY_READING_FREQUENCY_CAP,
                max(1, round(frequency * SECONDARY_READING_FREQUENCY_FACTOR)),
            )
            reading_frequency = max(
                reading_frequency,
                COMMON_SECONDARY_READING_FLOORS.get((word, spelling), 0),
            )
            entries.append((spelling, word, reading_frequency))
    entries.sort(key=lambda item: (item[0].replace("'", ""), -item[2], item[1]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as output:
        output.write("# segmented_pinyin<TAB>word<TAB>frequency\n")
        for spelling, word, frequency in entries:
            output.write(f"{spelling}\t{word}\t{frequency}\n")
    print(f"wrote {len(entries)} entries to {args.output}")


if __name__ == "__main__":
    main()
