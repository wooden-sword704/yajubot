"""ティア（等級）非依存項目「今日の拓也県旅行」の選択肢データ。

【追加方法】
`TAKUYA_TRAVEL_ITEMS` のタプルに文字列を1行ずつ追加するだけで簡単に選択肢を増やせます。
"""

from __future__ import annotations

RANDOM_CATEGORY_NAME: str = "今日の拓也県旅行"

TAKUYA_TRAVEL_ITEMS: tuple[str, ...] = (
    "新横浜",
    "下北沢",
    "三軒茶屋",
    "新宿二丁目",
    "川崎",
    "熱海",
    "箱根",
    "江ノ島",
)
