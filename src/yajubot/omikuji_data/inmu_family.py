"""ティア（等級）連動項目「今日の淫夢ファミリー」の選択肢データ。

【追加方法】
`INMU_FAMILY_BY_TIER` の各ティア（"超大吉" 〜 "凶"）のタプルに
文字列を1行ずつ追加するだけで簡単に選択肢を増やせます。
"""

from __future__ import annotations

TIER_CATEGORY_NAME: str = "今日の淫夢ファミリー"

INMU_FAMILY_BY_TIER: dict[str, tuple[str, ...]] = {
    "超大吉": (
        "野獣先輩（王道）",
        "遠野",
    ),
    "大吉": (
        "MUR",
        "KMR",
    ),
    "中吉": (
        "AKYS",
        "TNOK",
    ),
    "小吉": (
        "HTN",
        "平野店長",
    ),
    "末吉": (
        "虐待おじさん",
        "SZK",
    ),
    "凶": (
        "徳川",
        "変態糞親父",
    ),
}
