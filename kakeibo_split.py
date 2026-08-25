"""1回のユーザー入力から複数の家計簿取引候補を作るための分割・検証ロジック。

Section C確定仕様の安全境界は変更しない。すなわち、
  - amount と date は LLM 生成値を最終値として一切採用しない
  - 最終値は各取引の原文断片から kakeibo_amount / kakeibo_date で機械抽出する
  - 1取引 = 1確認 = 1 POST
このモジュールが担うのは「入口で複数候補へ分割してよいか」の判定だけで、
確認UI・送信・スレッドには一切依存しない純粋関数で構成する。

LLM が返す source_text は信用しない。原文に実在し、空でなく、原文上で
重複せず、順序を安全に再構築できることをコード側で検証する。検証に失敗した
場合は先頭だけ処理する部分成功を行わず、入力全体を拒否する。
"""
from kakeibo_amount import find_amount_spans
from kakeibo_confirmation import build_kakeibo_candidate
from kakeibo_date import find_explicit_date, find_explicit_dates
from prompt_builder import MAX_KAKEIBO_TRANSACTIONS_PER_INPUT

# 断片から金額を取り除いた残りが「区切りだけ」かどうかの判定に使う文字。
_SEPARATOR_CHARS = "、。,.・/-―ーとや及びおよび「」（）()[]【】:：;；"

__all__ = [
    "MAX_KAKEIBO_TRANSACTIONS_PER_INPUT",
    "normalize_transactions",
    "build_kakeibo_candidates",
]

# LLM が取引ごとに返してよいキー。amount と date は意図的に含めない
# (最終値を LLM に決めさせないため、受け取っても使わない)。
_ALLOWED_TRANSACTION_KEYS = frozenset(
    {"source_text", "store", "category", "type", "memo"}
)
_HORIZONTAL_SPACES = frozenset({" ", "　"})


def _clean_source_text(value) -> str:
    """source_text を検証用に正規化する。文字列でなければ空文字を返す。"""
    if not isinstance(value, str):
        return ""
    return value.strip()


def _locate_spans(fragments: list[str], user_text: str) -> list[tuple[int, int]] | None:
    """各断片の原文上の位置を確定する。確定できない場合は None を返す。

    まず LLM が返した順序のまま前方一致で走査する。この走査が成功すれば、
    順序が原文順であることと範囲が重複していないことが同時に保証される。

    走査に失敗した場合(順序が入れ替わっている等)は、各断片が原文中に
    ちょうど1回だけ出現するときに限り出現位置で並べ直す。出現回数が
    1回でない断片があると位置を一意に決められないため、その場合は
    推測せずに None を返す。
    """
    position = 0
    spans: list[tuple[int, int]] = []
    for fragment in fragments:
        index = user_text.find(fragment, position)
        if index < 0:
            spans = []
            break
        spans.append((index, index + len(fragment)))
        position = index + len(fragment)
    if spans:
        return spans

    # 順序を安全に再構築できるかを確認する。
    located: list[tuple[int, int]] = []
    for fragment in fragments:
        if user_text.count(fragment) != 1:
            return None
        index = user_text.index(fragment)
        located.append((index, index + len(fragment)))
    # 重複判定は位置順に並べた複製で行い、戻り値は fragments と同じ並びを保つ
    # (呼び出し側が spans[i] と fragments[i] の対応に依存しているため)。
    ordered = sorted(located)
    for (_, previous_end), (next_start, _) in zip(ordered, ordered[1:]):
        if next_start < previous_end:
            return None
    return located


def _find_all(text: str, fragment: str) -> list[int]:
    """fragment の全出現位置を、重なりを含めて返す。"""
    starts: list[int] = []
    position = 0
    while True:
        index = text.find(fragment, position)
        if index < 0:
            return starts
        starts.append(index)
        position = index + 1


def _remove_horizontal_spaces_with_map(text: str) -> tuple[str, list[int]]:
    """U+0020/U+3000だけを除き、各文字の原文位置を保持する。"""
    characters: list[str] = []
    source_indexes: list[int] = []
    for index, character in enumerate(text):
        if character in _HORIZONTAL_SPACES:
            continue
        characters.append(character)
        source_indexes.append(index)
    return "".join(characters), source_indexes


def _recover_horizontal_space_fragments(
    fragments: list[str], user_text: str
) -> tuple[list[str], list[tuple[int, int]]] | None:
    """半角/全角スペース差だけを許し、一意な原文断片とspanへ戻す。

    完全一致しなかった入力にだけ使う安全側fallback。句読点・数字・改行など
    U+0020/U+3000以外の文字は一切正規化しない。各断片の位置が1か所に決まり、
    spanが重複しない場合だけ、LLM文字列ではなく user_text の実スライスを返す。
    """
    compact_user_text, source_indexes = _remove_horizontal_spaces_with_map(
        user_text)
    recovered: list[str] = []
    spans: list[tuple[int, int]] = []

    for fragment in fragments:
        exact_starts = _find_all(user_text, fragment)
        if exact_starts:
            if len(exact_starts) != 1:
                return None
            start = exact_starts[0]
            end = start + len(fragment)
            original_fragment = fragment
        else:
            compact_fragment, _ = _remove_horizontal_spaces_with_map(fragment)
            if not compact_fragment:
                return None
            compact_starts = _find_all(compact_user_text, compact_fragment)
            if len(compact_starts) != 1:
                return None
            compact_start = compact_starts[0]
            compact_end = compact_start + len(compact_fragment)
            start = source_indexes[compact_start]
            end = source_indexes[compact_end - 1] + 1
            original_fragment = user_text[start:end]
            original_compact, _ = _remove_horizontal_spaces_with_map(
                original_fragment)
            if original_compact != compact_fragment:
                return None

        recovered.append(original_fragment)
        spans.append((start, end))

    ordered_spans = sorted(spans)
    for (_, previous_end), (next_start, _) in zip(
        ordered_spans, ordered_spans[1:]
    ):
        if next_start < previous_end:
            return None
    return recovered, spans


def normalize_transactions(transactions, user_text: str) -> dict:
    """LLM が返した取引候補列を検証し、原文順の断片とレコードへ正規化する。

    戻り値の "status":
      - "ok": "items" に (source_text, llm_record) を原文順で格納
      - "too_many": 件数が上限を超えた(入力全体を拒否する)
      - "invalid_split": 形状不正・原文に存在しない断片・重複・順序不明など
    """
    if not isinstance(transactions, list) or not transactions:
        return {"status": "invalid_split", "items": [], "spans": []}
    if len(transactions) > MAX_KAKEIBO_TRANSACTIONS_PER_INPUT:
        return {"status": "too_many", "items": []}

    fragments: list[str] = []
    records: list[dict] = []
    for entry in transactions:
        if not isinstance(entry, dict):
            return {"status": "invalid_split", "items": [], "spans": []}
        if not entry.keys() <= _ALLOWED_TRANSACTION_KEYS:
            return {"status": "invalid_split", "items": [], "spans": []}
        fragment = _clean_source_text(entry.get("source_text"))
        if not fragment:
            return {"status": "invalid_split", "items": [], "spans": []}
        fragments.append(fragment)
        records.append({k: v for k, v in entry.items() if k != "source_text"})

    if all(fragment in user_text for fragment in fragments):
        spans = _locate_spans(fragments, user_text)
        if spans is None:
            return {"status": "invalid_split", "items": [], "spans": []}
    else:
        recovered = _recover_horizontal_space_fragments(fragments, user_text)
        if recovered is None:
            return {"status": "invalid_split", "items": [], "spans": []}
        fragments, spans = recovered

    order = sorted(range(len(fragments)), key=lambda i: spans[i][0])
    items = [(fragments[i], records[i]) for i in order]
    ordered_spans = [(spans[i][0], spans[i][1]) for i in order]
    return {"status": "ok", "items": items, "spans": ordered_spans}


def _fragment_has_context(fragment: str, spans: list[tuple[int, int, int]]) -> bool:
    """断片が金額表現以外の実質的な内容を持つかどうかを判定する。

    「500円」のように金額だけの断片は、独立した取引なのか直前の取引の内訳なのかを
    機械的に決められない。LLMが「スーパーで2000円と500円」を人工的に切り分けた
    場合がこれに当たるため、単独取引として受理しない。
    区切り記号・空白しか残らない場合も文脈なしとみなす。
    """
    remainder = list(fragment)
    for start, end, _value in spans:
        for i in range(start, end):
            if 0 <= i < len(remainder):
                remainder[i] = ""
    text = "".join(remainder)
    return any(ch not in _SEPARATOR_CHARS and not ch.isspace() for ch in text)


def build_kakeibo_candidates(transactions, user_text: str, today=None) -> dict:
    """複数取引候補を構築する。1件でも処理不能なら入力全体を拒否する。

    戻り値の "status":
      - "ok": "candidates" に確認画面用の候補を原文順で格納
      - "too_many" / "invalid_split": normalize_transactions と同じ意味
      - "uncovered_amount": 原文に、どの取引断片にも含まれない金額表現が残っている
        (LLMが取引を取りこぼした可能性があるため入力全体を拒否する)
      - "ambiguous_split": 金額だけの断片など、独立した取引と機械的に確定できない
      - "ambiguous_date": 原文に明示日付が複数あり、日付を持たない断片へ
        どれを適用すべきか一意に決められない
      - "no_amount" / "invalid_amount_format" / "multiple_amounts":
        いずれかの断片で金額を一意に確定できなかった
        ("reason_index" にその断片の位置(0始まり)を格納)

    amount は各断片から kakeibo_amount が機械抽出する。date も各断片から機械抽出
    するが、断片自身が日付表現を持たない場合は、**原文全体の明示日付がちょうど
    1種類のときだけ**その日付へフォールバックする。複数種類ある場合は推測せず
    拒否する。どちらも無ければ従来どおり実行日を使う。新しい日付形式は
    追加していない。
    """
    normalized = normalize_transactions(transactions, user_text)
    if normalized["status"] != "ok":
        return {"status": normalized["status"], "candidates": []}

    items = normalized["items"]
    spans = normalized["spans"]
    single = len(items) == 1

    # --- 原文の金額をすべての断片が覆っているかを確認する ---
    # LLMが取引を取りこぼしても件数上限(10件)の判定だけでは検出できないため、
    # 原文側に残った金額表現がないかをコード側で確認する。
    whole_amounts = find_amount_spans(user_text)
    for start, end, _value in whole_amounts:
        covered = any(
            fragment_start <= start and end <= fragment_end
            for fragment_start, fragment_end in spans
        )
        if not covered:
            return {"status": "uncovered_amount", "candidates": []}

    # --- 断片ごとの金額数と文脈を確認する ---
    if not single:
        for index, (fragment, _record) in enumerate(items):
            fragment_start, fragment_end = spans[index]
            inner = [
                (s - fragment_start, e - fragment_start, v)
                for s, e, v in whole_amounts
                if fragment_start <= s and e <= fragment_end
            ]
            if len(inner) != 1:
                return {
                    "status": "multiple_amounts" if len(inner) > 1 else "no_amount",
                    "candidates": [],
                    "reason_index": index,
                }
            if not _fragment_has_context(fragment, inner):
                return {
                    "status": "ambiguous_split",
                    "candidates": [],
                    "reason_index": index,
                }

    # --- 日付フォールバックの一意性を確認する ---
    whole_dates = sorted(set(find_explicit_dates(user_text, today)))

    candidates = []
    for index, (fragment, llm_record) in enumerate(items):
        # 1件だけの場合は従来どおり入力全体を対象に抽出し、既存挙動を変えない。
        effective_text = user_text if single else fragment
        candidate = build_kakeibo_candidate(llm_record, effective_text)
        if candidate["status"] != "ok":
            return {
                "status": candidate["status"],
                "candidates": [],
                "reason_index": index,
            }
        if not single and find_explicit_date(fragment, today) is None:
            if len(whole_dates) == 1:
                candidate["date"] = whole_dates[0]
            elif len(whole_dates) > 1:
                return {
                    "status": "ambiguous_date",
                    "candidates": [],
                    "reason_index": index,
                }
        # 1件だけの場合は確認画面にも入力全文を出す。LLMが入力の一部分だけを
        # source_text として返しても、従来どおり原文全体が「認識・入力内容」へ
        # 表示されるようにするため(amount/dateの抽出元も入力全文のまま)。
        candidate["source_text"] = user_text if single else fragment
        candidates.append(candidate)

    return {"status": "ok", "candidates": candidates}
