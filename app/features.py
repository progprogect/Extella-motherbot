FEATURES: dict[str, dict] = {
    "mb_ai_assistant": {
        "name": "🤖 AI Ассистент",
        "desc": "Отвечает на любые вопросы с помощью GPT-4",
        "expert": "mb_ai_assistant",
        "params": {
            "__prompt_param__": "prompt",
            "model": "gpt-4o-mini",
            "system_prompt": "You are a helpful AI assistant. Respond in the same language as the user. Be concise and friendly.",
        },
    },
    "mb_translate": {
        "name": "🌐 Переводчик",
        "desc": "Переводит текст на нужный язык",
        "expert": "mb_translate_text",
        "params": {"__prompt_param__": "text", "target_lang": "en"},
    },
    "mb_content_gen": {
        "name": "✍️ Генератор контента",
        "desc": "Создаёт посты для соцсетей",
        "expert": "mb_generate_content",
        "params": {"__prompt_param__": "prompt", "platform": "telegram", "language": "Russian"},
    },
}
EXPERT_TO_FEATURE: dict[str, str] = {v["expert"]: k for k, v in FEATURES.items()}
