import datetime


KAKEIBO_EXPENSE_CATS = ["食費", "外食費", "日用品", "交通費", "衣服・美容",
                         "交際費", "趣味・娯楽", "医療費", "光熱費", "通信費",
                         "住居費", "保険", "教育費", "その他支出"]
KAKEIBO_INCOME_CATS  = ["給与", "賞与", "副収入", "お小遣い", "売却益", "還付金", "その他収入"]

# 1回のユーザー入力で受理する家計簿取引の上限。これを超える入力は先頭N件だけ
# 処理する部分成功を行わず、入力全体を拒否する。家計簿の共通語彙(カテゴリ一覧)と
# 同じ場所に置くことで、プロンプト生成側と分割検証側(kakeibo_split)の双方から
# 循環importなしで参照できるようにしている。
MAX_KAKEIBO_TRANSACTIONS_PER_INPUT = 10


class PromptInputTooLargeError(ValueError):
    """system prompt、入力、必要な履歴と予約量がcontext上限を超える。"""


class PromptBuilder:
    MODE_HINT = {
        "kakeibo": "\n\n[モード: 家計簿]",
        "health": "\n\n[モード: 健康管理]",
    }

    def __init__(self, system_prompt: str):
        self._system_prompt = system_prompt

    def build(
        self,
        user_text: str,
        session: dict,
        mode: str = "default",
        llm=None,
        n_ctx: int | None = None,
        max_tokens: int | None = None,
        token_cost_cache: dict | None = None,
        count_tokens_func=None,
        history_budget_ratio: float = 0.60,
        system_buf_tokens: int = 256,
        extra_reserved_tokens: int = 0,
        enforce_context_limit: bool = False,
        require_full_history: bool = False,
    ) -> list:
        history = session.get("history", [])
        summary = session.get("summary", "")

        sys_content = self._system_prompt + self.MODE_HINT.get(mode, "")

        if summary:
            sys_content += f"\n\n[要約]: {summary}"

        selected_history = history
        if (
            llm is not None
            and n_ctx is not None
            and max_tokens is not None
            and count_tokens_func is not None
        ):
            cache = token_cost_cache if token_cost_cache is not None else {}
            sys_tokens = count_tokens_func(llm, sys_content) + system_buf_tokens
            user_tokens = count_tokens_func(llm, user_text)
            fixed_tokens = (
                max_tokens
                + sys_tokens
                + user_tokens
                + max(0, extra_reserved_tokens)
            )
            if enforce_context_limit and fixed_tokens > n_ctx:
                raise PromptInputTooLargeError(
                    "system prompt、入力、応答予約を合わせると"
                    f"context上限を超えます（{fixed_tokens} > {n_ctx} tokens）。"
                )

            def _history_cost(h: dict) -> int:
                user = h.get("user", "")
                assistant = h.get("assistant", "")
                key = (user, assistant)
                cost = cache.get(key)
                if cost is None:
                    cost = (
                        count_tokens_func(llm, user)
                        + count_tokens_func(llm, assistant)
                        + 12
                    )
                    cache[key] = cost
                return cost

            if require_full_history:
                full_history_tokens = sum(
                    _history_cost(h) for h in history)
                total_tokens = fixed_tokens + full_history_tokens
                if enforce_context_limit and total_tokens > n_ctx:
                    raise PromptInputTooLargeError(
                        "保持中の添付、会話履歴、system prompt、応答予約を"
                        "合わせるとcontext上限を超えます"
                        f"（{total_tokens} > {n_ctx} tokens）。"
                    )
                selected_history = history
            else:
                budget = int(
                    (n_ctx - fixed_tokens)
                    * history_budget_ratio
                )
                budget = max(0, budget)

                selected_history = []
                for h in reversed(history):
                    cost = _history_cost(h)
                    if budget - cost < 0:
                        break
                    selected_history.insert(0, h)
                    budget -= cost

        msgs = [{"role": "system", "content": sys_content}]

        for h in selected_history:
            msgs.append({"role": "user", "content": h.get("user", "")})
            msgs.append({"role": "assistant", "content": h.get("assistant", "")})

        msgs.append({"role": "user", "content": user_text})
        return msgs

    def build_kakeibo_prompt(self, user_text: str) -> str:
        today = datetime.date.today().isoformat()
        exp_cats = "、".join(KAKEIBO_EXPENSE_CATS)
        inc_cats = "、".join(KAKEIBO_INCOME_CATS)
        limit = MAX_KAKEIBO_TRANSACTIONS_PER_INPUT
        return (
            f"{user_text}\n\n"
            f"---\n"
            f"上記のメッセージに含まれる取引を抽出し、"
            f"必ず以下の形式でJSONオブジェクトを1個だけ出力し、"
            f"前後に説明文を付けないでください。\n"
            f"今日の日付: {today}\n\n"
            f'```json\n'
            f'{{\n'
            f'  "transactions": [\n'
            f'    {{\n'
            f'      "source_text": "",\n'
            f'      "store": null,\n'
            f'      "category": null,\n'
            f'      "type": null,\n'
            f'      "memo": null\n'
            f'    }}\n'
            f'  ]\n'
            f'}}\n'
            f'```\n\n'
            f"取引が1件だけの場合も transactions の要素を1個にして出力してください。\n"
            f"source_text には、その取引に対応する元入力の連続する部分文字列を入れてください。\n"
            f"一字一句変更せず、半角スペース(U+0020)・全角スペース(U+3000)の"
            f"削除・追加・置換、句読点・カンマ・改行・数字の変更をしないでください。\n"
            f"要約・言い換え・表記の整形・補完をせず、"
            f"原文に存在しない文字を追加しないでください。\n"
            f"例: 原文が「セリア、8月20日、1,430円 日用品」なら、"
            f"正しいsource_textも「セリア、8月20日、1,430円 日用品」です。"
            f"「セリア、8月20日、1,430円日用品」は半角スペースを削除しているため禁止です。\n"
            f"取引が1件だけの場合は source_text に入力文全体をそのまま入れてください。\n"
            f"複数の取引がある場合は、source_text どうしが重ならないようにし、"
            f"入力文に出てくる順番で並べてください。\n"
            f"金額と日付はアプリ側が入力文から抽出するため、"
            f"amount と date は出力しないでください。\n"
            f"判定できたフィールドだけ値を設定し、それ以外は null のままにしてください。\n"
            f"type は必ず「支出」または「収入」のどちらかにしてください"
            f"(それ以外の文字列は使わないでください)。\n"
            f"category は次の一覧の中から1つだけ選んでください"
            f"(一覧にない値は使わないでください)。\n"
            f"支出カテゴリ: {exp_cats}\n"
            f"収入カテゴリ: {inc_cats}\n"
            f"取引が{limit}件を超える場合はJSONを出力せず、"
            f"「一度に登録できる取引は最大{limit}件です」と"
            f"自然文だけで案内してください。"
        )

    def build_health_prompt(self, user_text: str) -> str:
        today = datetime.date.today().isoformat()
        return (
            f"{user_text}\n\n"
            f"---\n"
            f"必ず最初に以下のJSON形式で健康記録データを出力し、その後に自然な返答を1〜2文だけ続けてください。\n"
            f"今日の日付: {today}\n\n"
            f'```json\n'
            f'{{"date": "{today}", "weight": 体重(kg, 不明はnull), '
            f'"body_fat": 体脂肪率(%, 不明はnull), '
            f'"muscle_mass": 筋肉量(kg, 不明はnull), '
            f'"bmr": 基礎代謝量(kcal整数, 不明はnull), '
            f'"temperature": 体温(℃, 不明はnull), '
            f'"pulse": 脈拍(bpm整数, 不明はnull), '
            f'"systolic_bp": 収縮期血圧(mmHg整数, 不明はnull), '
            f'"diastolic_bp": 拡張期血圧(mmHg整数, 不明はnull), '
            f'"meal_detail": "食べた食品名（ユーザーの発言通りの表記、不明はnull）", '
            f'"activity_log": "実際に行った運動・作業・外出など（ユーザーの発言通りの表記、不明はnull）", '
            f'"memo": "メモ・備考として明示された内容（ユーザーの発言通りの表記、不明はnull）"}}\n'
            f'```\n\n'
            f"数値が不明な場合は null を使用。"
            f"meal_detail・activity_log・memo は、該当する内容が発言にある場合だけユーザーの単語をそのまま使い、言い換え・造語・変換を禁止します。"
            f"食事・行動・メモだけの入力も有効な健康記録です。該当内容が1つでもあれば全項目をnullにしないでください。"
            f'例:「食事ログ コーヒー 水 メモ テストデータ」なら "meal_detail":"コーヒー 水", "memo":"テストデータ" とします。'
            f"体重・体脂肪率・体温・脈拍・血圧・筋肉量・基礎代謝などの測定文を activity_log に複製しないでください。"
            f"同じ測定文を memo にも複製しないでください。memoは「メモ」または「メモ追加」で明示された内容だけにしてください。"
            f"入力が測定値だけの場合は activity_log と memo を null にしてください。"
            f'例:「体脂肪率17.9%」なら "body_fat":17.9, "activity_log":null, "memo":null とします。'
        )

    def build_health_extraction_prompt(self, user_text: str) -> str:
        today = datetime.date.today().isoformat()
        return (
            f"次の発言から健康記録を抽出し、説明を付けずJSONオブジェクト1個だけを出力してください。\n"
            f"発言: {user_text}\n"
            f"日付: {today}\n"
            f'{{"date":"{today}","weight":null,"body_fat":null,'
            f'"muscle_mass":null,"bmr":null,"temperature":null,'
            f'"pulse":null,"systolic_bp":null,"diastolic_bp":null,'
            f'"meal_detail":null,"activity_log":null,"memo":null}}\n'
            f"weight=体重kg、body_fat=体脂肪率%、muscle_mass=筋肉量kg、"
            f"bmr=基礎代謝kcal、temperature=体温℃、pulse=脈拍bpm、"
            f"systolic_bp=上の血圧、diastolic_bp=下の血圧です。"
            f"発言にない値はnullにしてください。食事と実際の運動・作業・外出だけを各ログへ入れ、"
            f"メモ・備考として明示された内容だけをmemoへ原文のまま入れてください。"
            f"食事・行動・メモだけの入力も有効です。該当内容が1つでもあれば全項目をnullにしないでください。"
            f'例:「食事ログ コーヒー 水 メモ テストデータ」なら "meal_detail":"コーヒー 水", "memo":"テストデータ" とします。'
            f"測定文をactivity_logやmemoへ複製しないでください。memoは「メモ」または「メモ追加」で明示された内容だけにしてください。"
            f'測定値だけの発言ではactivity_logとmemoをnullにしてください。例:「体脂肪率17.9%」ならbody_fat=17.9、activity_log=null、memo=nullです。'
        )
