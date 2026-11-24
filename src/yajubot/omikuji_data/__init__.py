"""おみくじのティア定義と項目別選択肢データ。

【構成】
- ティア（等級）で絞られる項目（1項目のみ）: `inmu_family.py`（「今日の淫夢ファミリー」）
- ティアに関係なくランダムに選ばれる項目:
  - `goroku.py`（「今日の語録」）
  - `takuya_travel.py`（「今日の拓也県旅行」）

新しいランダム項目を追加したい場合は、`omikuji_data/` 配下に新しいファイルを作成し、
このファイルの `OMIKUJI_RANDOM_CATEGORIES` に登録してください。
"""

from __future__ import annotations

from dataclasses import dataclass

from yajubot.omikuji_data.goroku import GOROKU_ITEMS
from yajubot.omikuji_data.goroku import RANDOM_CATEGORY_NAME as GOROKU_CATEGORY_NAME
from yajubot.omikuji_data.inmu_family import INMU_FAMILY_BY_TIER, TIER_CATEGORY_NAME
from yajubot.omikuji_data.takuya_travel import RANDOM_CATEGORY_NAME as TAKUYA_TRAVEL_CATEGORY_NAME
from yajubot.omikuji_data.takuya_travel import TAKUYA_TRAVEL_ITEMS

# Embed に表示するティア連動項目名（等級で絞られるのはこの1項目のみ）
OMIKUJI_CATEGORIES: tuple[str, ...] = (TIER_CATEGORY_NAME,)


@dataclass(frozen=True, slots=True)
class OmikujiTier:
    """おみくじのティア（等級）定義。

    Attributes:
        name: 運勢（ティア名。例: "超大吉", "大吉"）
        color: Embed のカラーコード (16進数)
        probability: このティアが選ばれる確率（%）。合計が 100 でない場合は自動スケーリングされる。
        categories: 項目名 -> 選択肢タプル のマッピング
    """

    name: str
    color: int
    probability: float
    categories: dict[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class OmikujiRandomCategory:
    """ティア（等級）に関係なくランダムに抽選される項目定義。"""

    name: str
    items: tuple[str, ...]


# 初期設定: 6ティア（均等確率。必要に応じて probability を編集してください）
OMIKUJI_TIERS: tuple[OmikujiTier, ...] = (
    OmikujiTier(
        name="# お　お　あ　た　り",
        color=0xFF69B4,
        probability=0.5,        categories={TIER_CATEGORY_NAME: INMU_FAMILY_BY_TIER["おおあたり"]},
    ),
    OmikujiTier(
        name="ビリー兄貴級",
        color=0xFFD700,
        probability=10,
        categories={TIER_CATEGORY_NAME: INMU_FAMILY_BY_TIER["ビリー"]},
    ),
    OmikujiTier(
        name="野獣先輩級",
        color=0x2ECC71,
        probability=30,
        categories={TIER_CATEGORY_NAME: INMU_FAMILY_BY_TIER["野獣"]},
    ),
    OmikujiTier(
        name="MUR大先輩級",
        color=0x3498DB,
        probability=30,
        categories={TIER_CATEGORY_NAME: INMU_FAMILY_BY_TIER["MUR"]},
    ),
    OmikujiTier(
        name="拓也さん級",
        color=0x95A5A6,
        probability=25,
        categories={TIER_CATEGORY_NAME: INMU_FAMILY_BY_TIER["拓也"]},
    ),
    OmikujiTier(
        name="# <a:gabigabipinky:1574738391751336008> お　お　は　ず　れ <a:gabigabipinky:1574738391751336008>",
        color=0xE74C3C,
        probability=5,
        categories={TIER_CATEGORY_NAME: INMU_FAMILY_BY_TIER["ピンキー"]},
    ),
)

# ティアに関係なくランダムに選ばれる項目一覧
OMIKUJI_RANDOM_CATEGORIES: tuple[OmikujiRandomCategory, ...] = (
    OmikujiRandomCategory(name=GOROKU_CATEGORY_NAME, items=GOROKU_ITEMS),
    OmikujiRandomCategory(name=TAKUYA_TRAVEL_CATEGORY_NAME, items=TAKUYA_TRAVEL_ITEMS),
)

__all__ = [
    "OMIKUJI_CATEGORIES",
    "OMIKUJI_RANDOM_CATEGORIES",
    "OMIKUJI_TIERS",
    "OmikujiRandomCategory",
    "OmikujiTier",
]
