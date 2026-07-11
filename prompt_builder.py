import datetime


KAKEIBO_EXPENSE_CATS = ["食費", "外食費", "日用品", "交通費", "衣服・美容",
                         "交際費", "趣味・娯楽", "医療費", "光熱費", "通信費",
                         "住居費", "保険", "教育費", "その他支出"]
KAKEIBO_INCOME_CATS  = ["給与", "賞与", "副収入", "お小遣い", "売却益", "還付金", "その他収入"]


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
            budget = int(
                (n_ctx - max_tokens - sys_tokens - user_tokens)
                * history_budget_ratio
            )
            budget = max(0, budget)

            selected_history = []
            for h in reversed(history):
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
        return (
            f"{user_text}\n\n"
            f"---\n"
            f"上記のメッセージに対して自然に返答した後、必ず以下のJSON形式で家計簿データを出力してください。\n"
            f"今日の日付: {today}\n\n"
            f'```json\n'
            f'{{"date": "{today}", "store": "店名(不明は空文字)", '
            f'"amount": 金額(整数), "category": "カテゴリ名", '
            f'"type": "支出", "memo": "メモ(省略可)"}}\n'
            f'```\n\n'
            f"支出カテゴリ: {exp_cats}\n"
            f"収入カテゴリ: {inc_cats}\n"
            f"金額・店名が不明な場合は null を使用してください。"
        )

    def build_health_prompt(self, user_text: str) -> str:
        today = datetime.date.today().isoformat()
        return (
            f"{user_text}\n\n"
            f"---\n"
            f"上記のメッセージに対して自然に返答した後、必ず以下のJSON形式で健康記録データを出力してください。\n"
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
            f'"activity_log": "運動・作業・出来事など（ユーザーの発言通りの表記、不明はnull）"}}\n'
            f'```\n\n'
            f"数値が不明な場合は null を使用。"
            f"meal_detail・activity_log はユーザーの発言に含まれる単語をそのまま使い、言い換え・造語・変換を禁止します。"
        )
