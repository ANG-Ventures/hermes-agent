"""``hermes model`` subcommand parser.

Extracted verbatim from ``hermes_cli/main.py:main()`` (god-file Phase 2).
Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_model_parser(subparsers, *, cmd_model: Callable) -> None:
    """Attach the ``model`` subcommand to ``subparsers``."""
    # =========================================================================
    # model command
    # =========================================================================
    model_parser = subparsers.add_parser(
        "model",
        help="Select default model and provider",
        description="Interactively select your inference provider and default model",
    )
    model_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Wipe the model picker disk cache and re-fetch every provider's live /v1/models list.",
    )
    model_parser.add_argument(
        "--portal-url",
        help="Portal base URL for Nous login (default: production portal)",
    )
    model_parser.add_argument(
        "--inference-url",
        help="Inference API base URL for Nous login (default: production inference API)",
    )
    model_parser.add_argument(
        "--client-id",
        default=None,
        help="OAuth client id to use for Nous login (default: hermes-cli)",
    )
    model_parser.add_argument(
        "--scope", default=None, help="OAuth scope to request for Nous login"
    )
    model_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not attempt to open the browser automatically during Nous login",
    )
    model_parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="HTTP request timeout in seconds for Nous login (default: 15)",
    )
    model_parser.add_argument(
        "--ca-bundle", help="Path to CA bundle PEM file for Nous TLS verification"
    )
    model_parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS verification for Nous login (testing only)",
    )
    model_parser.add_argument("--chat", help="Pin a destination chat (platform:chat_id), not the profile default")
    action = model_parser.add_mutually_exclusive_group()
    action.add_argument("--set", dest="chat_model", metavar="MODEL", help="Model or alias to pin with --chat")
    action.add_argument("--clear", action="store_true", help="Clear the --chat model pin")
    model_parser.add_argument("--provider", help="Provider for --chat --set")
    model_parser.set_defaults(func=cmd_model)


def set_chat_model(args) -> None:
    """Explicit CLI destination; no current-session or home-channel guessing."""
    from gateway.chat_model_pins import ChatModelPins
    from gateway.config import Platform, load_gateway_config
    from gateway.session import SessionSource, SessionStore
    from hermes_cli.config import load_config
    from hermes_cli.model_switch import switch_model

    platform, separator, chat_id = args.chat.partition(":")
    if not separator or not chat_id or bool(args.chat_model) == bool(args.clear):
        raise SystemExit("Use --chat platform:chat_id with exactly one of --set MODEL or --clear")
    try:
        source = SessionSource(platform=Platform(platform), chat_id=chat_id)
    except ValueError:
        raise SystemExit(f"Unknown chat platform: {platform}") from None
    config = load_gateway_config()
    identity = None
    if not args.clear:
        raw = load_config()
        model = raw.get("model", {})
        model = model if isinstance(model, dict) else {"default": model}
        result = switch_model(
            raw_input=args.chat_model,
            current_model=model.get("default", ""),
            current_provider=model.get("provider", "auto"),
            explicit_provider=args.provider or "",
            is_global=False,
            user_providers=raw.get("providers"),
            custom_providers=raw.get("custom_providers"),
        )
        if not result.success:
            raise SystemExit(result.error_message)
        identity = {"model": result.new_model, "provider": result.target_provider, "api_mode": result.api_mode}
    store = SessionStore(sessions_dir=config.sessions_dir, config=config)
    store._ensure_loaded()
    namespace = store._generate_session_key(source).split(":")[1]
    ChatModelPins(config.sessions_dir).set(namespace, platform, chat_id, identity)
    print(f"Chat model pin {'cleared' if args.clear else 'saved'} for {args.chat}.")
