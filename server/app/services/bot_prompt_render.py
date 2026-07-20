"""Format a bot-prompt dict into the plain-text layout More-Than uses.

Produces:
  main_prompt_text  — the אישיות / מטרה / מידע נוסף document (what you paste into the bot builder)
  knowledge_bases   — the list of {name, content} (each becomes its own Drive doc)

Kept text-only on purpose: the prompt is copied into the WhatsApp-bot platform,
so plain, unambiguous text beats any rich format.
"""


def format_main_prompt(bot: dict) -> str:
    """Assemble the אישיות/מטרה/מידע נוסף document from a generate_bot_prompt() dict."""
    parts: list[str] = []

    personality = (bot.get("personality") or "").strip()
    goal = (bot.get("goal") or "").strip()
    extra = (bot.get("extra_info") or "").strip()

    parts.append("אישיות:\n" + personality)
    parts.append("מטרה:\n" + goal)
    parts.append("מידע נוסף:\n" + extra)

    kbs = bot.get("knowledge_bases") or []
    if kbs:
        names = "\n".join(f"- {kb.get('name', '').strip()}" for kb in kbs if kb.get("name"))
        parts.append("בסיסי ידע (בקבצים נפרדים):\n" + names)

    return "\n\n".join(p.strip() for p in parts if p.strip()) + "\n"


def knowledge_base_files(bot: dict) -> list[dict]:
    """Return [{name, content}] for each knowledge base, ready to upload."""
    out: list[dict] = []
    for kb in bot.get("knowledge_bases") or []:
        name = (kb.get("name") or "").strip()
        content = (kb.get("content") or "").strip()
        if name and content:
            out.append({"name": name, "content": content})
    return out
