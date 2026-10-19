"""Operation ceilings shared by presentation and admission."""
ASK_OPERATIONS = frozenset({"file_read", "internal_read", "network_read", "user_interaction"})
PLAN_OPERATIONS = ASK_OPERATIONS | frozenset({"plan_update"})
