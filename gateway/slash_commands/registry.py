"""Canonical slash-command name -> ``GatewayRunner`` handler *method name*.

Pure data. Nothing here executes a handler and nothing here duplicates
``CommandDef`` — name, aliases, category, args hint and the ``cli_only`` /
``gateway_only`` / ``gateway_config_gate`` flags all stay in
``hermes_cli/commands.py``'s ``COMMAND_REGISTRY``, which remains the single
source of truth for command *metadata*. This table carries exactly one string
per command: the binding that ``gateway/run.py`` previously spelled out by hand,
once per branch.

The value is the method **name**, not a function object, on purpose:

* dispatch stays ``getattr(self, name)(event)``, i.e. still a bound method on
  the ``GatewayRunner``, so ``self`` is unchanged and behavior is identical;
* this module imports nothing from the leaf modules, so it can neither create an
  import cycle nor force eager import of all 16 of them.

Only branches of the literal form ``if canonical == "x": return await
self._handle_x_command(event)`` are bound here. ``gateway/run.py`` keeps every
non-uniform branch (``/new``, ``/start``, ``/egress``, ``/learn``,
``/blueprint``, ``/undo``, ``/queue``, ``/steer``, ``/moa``) as an explicit
branch evaluated *before* this table, and ``/suggestions`` stays explicit because
its handler lives in ``run.py`` rather than in this package.
"""

from __future__ import annotations

GATEWAY_SLASH_HANDLERS: dict[str, str] = {
    "agents": "_handle_agents_command",
    "approve": "_handle_approve_command",
    "background": "_handle_background_command",
    "branch": "_handle_branch_command",
    "bundles": "_handle_bundles_command",
    "codex-runtime": "_handle_codex_runtime_command",
    "commands": "_handle_commands_command",
    "compress": "_handle_compress_command",
    "debug": "_handle_debug_command",
    "deny": "_handle_deny_command",
    "fast": "_handle_fast_command",
    "footer": "_handle_footer_command",
    "goal": "_handle_goal_command",
    "help": "_handle_help_command",
    "insights": "_handle_insights_command",
    "kanban": "_handle_kanban_command",
    "memory": "_handle_memory_command",
    "model": "_handle_model_command",
    "personality": "_handle_personality_command",
    "platform": "_handle_platform_command",
    "profile": "_handle_profile_command",
    "reasoning": "_handle_reasoning_command",
    "reload-mcp": "_handle_reload_mcp_command",
    "reload-skills": "_handle_reload_skills_command",
    "restart": "_handle_restart_command",
    "resume": "_handle_resume_command",
    "retry": "_handle_retry_command",
    "rollback": "_handle_rollback_command",
    "sessions": "_handle_sessions_command",
    "sethome": "_handle_set_home_command",
    "skills": "_handle_skills_command",
    "status": "_handle_status_command",
    "stop": "_handle_stop_command",
    "subgoal": "_handle_subgoal_command",
    "title": "_handle_title_command",
    "topic": "_handle_topic_command",
    "topup": "_handle_topup_command",
    "update": "_handle_update_command",
    "usage": "_handle_usage_command",
    "verbose": "_handle_verbose_command",
    "version": "_handle_version_command",
    "voice": "_handle_voice_command",
    "whoami": "_handle_whoami_command",
    "yolo": "_handle_yolo_command",
}
