"""Prompt construction.

Two passes:

* **split**  — meeting metadata + topic segmentation + a project guess per
  segment. A 昼会 routinely covers several projects, so this pass is what makes
  per-project ledgers possible at all.
* **detail** — deep extraction for one segment at a time. Narrower context
  produces markedly better decisions/actions/signals than asking for everything
  about everything in one shot.
"""

from __future__ import annotations

from ..models import SpeakerStats
from ..registry.store import Registry

SIGNAL_GUIDE = """\
### 温度感(signals)の記録方針
発言の「熱量・言い方・こだわり」は、後から資料を作ったり相手の関心を読むときに効きます。
ただし**特筆すべき場合のみ**記録してください。淡々と進んだ議題では signals は空配列で構いません。

kind は次から選びます:
- escalation : 声を荒げた、語気が強まった、明らかに苛立った
- resistance : はっきり反対した、押し返した
- enthusiasm : 前のめり、強い賛意、自ら手を挙げた
- hesitation : 言い淀んだ、条件付きでしか同意しなかった
- silence    : 本来意見を言うはずの人が黙った、返答が無かった
- repetition : 同じ主張を繰り返した(= その人にとって重要度が高い)
- deflection : 明言を避けた、話題を逸らした、判断を保留した
- concern    : 温度は高くないが明確な懸念を表明した

各 signal には必ず:
- topic   : 何について (短い名詞句。例「納期」「外注費」)
- evidence: どこでどう表れたかの客観的記述。解釈ではなく観察を書く
- concern : その裏にある関心事(推測になる場合は confidence を下げる)
- quote   : 逐語引用があれば
不明な点を埋めるために創作しないでください。書けないものは書かない。
"""

MIC_GUIDE_SHARED = """\
### 重要: 話者の帰属について
この議事録は**共通マイクで収録された可能性が高い**と機械的に判定されました
(発言の大半が単一の話者ラベルに寄っています)。
したがって「誰が言ったか」は信用できません。
- signals / decisions / actions の speaker・owner・raised_by は原則 **null** にしてください
- 発言内容そのもの(何が論点で、何が決まり、何が懸念か)の抽出に集中してください
- 文中で明示的に名前が呼ばれている場合(例「田中さんお願いします」)に限り、その人物を記載して構いません
"""

MIC_GUIDE_RELIABLE = """\
### 話者の帰属について
話者ラベルは信頼できると判定されています。speaker / owner / raised_by は
分かる範囲で記入してください。ただし推測が混ざる場合は confidence を下げてください。
"""


def _mic_guide(stats: SpeakerStats) -> str:
    guide = MIC_GUIDE_RELIABLE if stats.attribution == "reliable" else MIC_GUIDE_SHARED
    if stats.distribution:
        top = list(stats.distribution.items())[:5]
        dist = ", ".join(f"{k}: {v:.0%}" for k, v in top)
        guide += f"\n(機械計測による発言比率: {dist} / 判定: {stats.mic_mode})\n"
    if stats.note:
        guide += f"({stats.note})\n"
    return guide


def _context_block(registry: Registry) -> str:
    parts = [f"## 登録済みの案件一覧(この id 以外を project_id に使わないこと)\n{registry.digest()}"]
    people = registry.people_digest()
    if not people.startswith("("):
        parts.append(f"## 登録済みの人物\n{people}")
    glossary = registry.glossary_digest()
    if glossary:
        parts.append(f"## 社内用語\n{glossary}")
    return "\n\n".join(parts)


def split_prompt(text: str, registry: Registry, stats: SpeakerStats, hint: str = "") -> str:
    return f"""\
あなたは社内の議事録を構造化する専門アシスタントです。
以下の議事録を読み、(1)会議のメタ情報 と (2)話題ごとのセグメント分割 を出力してください。

## セグメント分割の方針
- **1つの会議で複数の案件が話される**ことが普通です(特に昼会・定例)。
  案件が変われば必ずセグメントを分けてください。
- 同じ案件でも論点が明確に切り替わるなら分けて構いません。逆に細切れにしすぎないこと。
- 雑談・アイスブレイク・接続確認だけの部分はセグメントにしないでください。
- span には、その話題が転記のどこから始まるかの手がかり(見出し・時刻・冒頭の一文)を書いてください。
- segment_id は "s1", "s2", ... と連番で付けてください。

## 案件の推定(guess)
- 各セグメントについて、下記の登録済み案件のどれに該当するかを判断し、
  guess.project_id に **既存の id をそのまま** 入れてください。
- 該当する案件が無い / 判断できない場合は project_id を null にし、
  guess.suggested_name に新規案件名の案を書いてください。**id を勝手に作らないでください。**
- guess.confidence は 0.0-1.0 で、根拠の強さを正直に付けてください。曖昧なときに高い値を付けないこと。

## この pass では
decisions / actions / open_questions / signals は**空のままで構いません**(後段で個別に抽出します)。
title・summary・span・guess・speakers に集中してください。

{_context_block(registry)}

{_mic_guide(stats)}
{hint}

## 議事録
<transcript>
{text}
</transcript>
"""


def detail_prompt(
    text: str,
    segment_title: str,
    segment_span: str,
    project_name: str | None,
    registry: Registry,
    stats: SpeakerStats,
) -> str:
    scope = f"「{segment_title}」"
    if segment_span:
        scope += f" (この話題は転記中の『{segment_span}』付近から始まります)"
    project_line = (
        f"このセグメントは案件「{project_name}」に関するものと判定されています。\n"
        if project_name
        else "このセグメントに対応する案件はまだ確定していません。\n"
    )
    return f"""\
あなたは社内の議事録を構造化する専門アシスタントです。
以下の議事録のうち、**{scope} の話題に関する部分だけ**を対象に、構造化情報を抽出してください。
対象外の話題については何も出力しないでください。

{project_line}
## 抽出するもの
- summary       : この話題で何が起きたかの要約(3-5文)。決まったこと・揉めたこと・残ったことが分かるように。
- decisions     : 決まったこと。決まっていないものを決定として書かないこと。
- actions       : 誰が何をいつまでに。owner/due が不明なら null。
- open_questions: 未解決の論点。blocks には「これが決まらないと何が止まるか」を書く。
- risks         : 表明された懸念・リスク。
- next_agenda   : 次回話すべきと明示された事項。
- signals       : 下記の方針に従って。

{SIGNAL_GUIDE}

{_mic_guide(stats)}

## 参考: 登録済みの人物
{registry.people_digest()}

## 議事録
<transcript>
{text}
</transcript>
"""
