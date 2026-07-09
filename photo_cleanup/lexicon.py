"""Word lists for content-based screenshot classification (English + Czech).

Fully on-device: matched against Apple's stored OCR tokens. Edit freely —
these are meant to be tuned to your own screenshots.
"""

# --- AUTHORITATIVE app identity (decided by the app, overrides scoring) ------
# A screenshot OF one of these private social/chat apps is ALWAYS kept.
PRIVATE_APPS = {
    "whatsapp", "instagram", "insta", "facebook", "fb", "messenger",
    "snapchat", "telegram", "imessage", "facetime", "tiktok", "viber", "signal",
}
# A screenshot OF one of these work chat apps is ALWAYS proposed for removal.
WORK_CHAT_APPS = {
    "slack", "teams", "webex", "mattermost",
}

# --- WORK: apps & tools (strong signal, weight 3 => one match clears the bar) -
WORK_APPS = {
    "slack", "jira", "confluence", "github", "gitlab", "bitbucket", "figma",
    "notion", "trello", "asana", "miro", "linear", "zoom", "teams", "webex",
    "outlook", "gmail", "excel", "powerpoint", "keynote", "sheets", "word",
    "jenkins", "datadog", "grafana", "kibana", "looker", "tableau", "salesforce",
    "hubspot", "stripe", "intercom", "zendesk", "metabase", "odoo",
    "bigquery", "snowflake", "airflow", "kubernetes", "docker", "terraform",
    # the user's own work products / clients (fintech app by Ooredoo)
    "walletii", "ooredoo",
}

# --- WORK: dev / technical vocabulary (weight 1) ----------------------------
WORK_DEV = {
    "localhost", "console", "terminal", "error", "exception", "stacktrace",
    "npm", "yarn", "git", "commit", "merge", "branch", "pull", "request",
    "deploy", "deployment", "build", "pipeline", "api", "endpoint", "function",
    "const", "import", "export", "return", "select", "query", "database", "db",
    "server", "http", "https", "json", "sql", "repo", "repository", "config",
    "log", "logs", "debug", "null", "undefined", "async", "await", "schema",
    # mobile / app dev & app-store / review context
    "app", "apps", "android", "ios", "appstore", "playstore", "testflight",
    "sdk", "release", "version", "crash", "guidelines", "notifications",
    "feature", "backend", "frontend", "staging", "production", "qa", "device",
    "bug", "ticket", "rollout", "changelog", "patch",
}

# --- WORK: business vocabulary EN + CZ (weight 1) ---------------------------
WORK_BIZ = {
    # english
    "invoice", "budget", "revenue", "profit", "cost", "kpi", "dashboard",
    "report", "reporting", "meeting", "agenda", "deadline", "project", "client",
    "contract", "proposal", "presentation", "quarter", "roadmap", "milestone",
    "stakeholder", "vat", "iban", "payment", "order", "offer", "campaign",
    "metrics", "analytics", "conversion", "onboarding", "backlog", "sprint",
    "ticket", "standup", "retro", "okr", "okrs", "headcount", "payroll",
    # czech
    "faktura", "faktury", "smlouva", "smlouvy", "objednávka", "nabídka",
    "rozpočet", "schůzka", "porada", "projekt", "klient", "klienti", "zákazník",
    "zákazníci", "prezentace", "dodavatel", "dodávka", "dph", "úhrada",
    "platba", "kampaň", "termín",
}

# --- PRIVATE: messaging-app UI markers (weight 2) ---------------------------
# Only app-SPECIFIC markers. Deliberately excludes generic words like
# "message"/"reply"/"sent"/"read"/"online" — those appear in work tools
# (Slack, email, app dashboards) and caused work screenshots to read private.
PRIVATE_UI = {
    "imessage", "whatsapp", "messenger", "telegram", "snapchat", "facetime",
    "delivered", "edited", "unsent", "reacted", "typing",
}

# --- PRIVATE: casual / intimate / relational EN + CZ (weight 1) -------------
PRIVATE_CASUAL = {
    # english casual / affection
    "love", "miss", "babe", "baby", "honey", "kiss", "hug", "xoxo", "lol",
    "haha", "hahaha", "omg", "cute", "sorry", "sweetheart", "darling", "dear",
    "goodnight", "morning", "dinner", "tonight", "weekend", "tired", "sleep",
    # czech casual / affection / pronouns (very common in personal chat)
    "ahoj", "čau", "miluju", "líbám", "líbat", "pusu", "pusinka", "zlato",
    "lásko", "miláčku", "mami", "tati", "babi", "děkuju", "díky", "prosím",
    "promiň", "máš", "mám", "dneska", "večer", "ráno", "spát", "doma", "holka",
    "kluk", "ja", "já", "ty", "mě", "tě", "tebe", "mi", "jsem", "jsi", "bych",
    "bys", "kdyby", "asi", "jako", "dobrej", "hezky", "hrozne", "fakt",
}
