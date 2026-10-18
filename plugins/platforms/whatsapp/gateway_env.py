"""WhatsApp (Baileys bridge) env -> gateway config bridge (``PlatformEntry.env_overrides``)."""

from gateway.config import GatewayConfig, Platform, _getenv
from gateway.platform_env import enable_from_env, home_step
from utils import is_truthy_value


def _enabled_flag(config: GatewayConfig) -> None:
    """WhatsApp (Baileys bridge) uses a flag, not credentials. WHATSAPP_ENABLED=false overrides YAML;
    WHATSAPP_ENABLED=true follows the credential contract — it never beats an explicit YAML disable
    (the dashboard's disable action writes only ``platforms.whatsapp.enabled: false`` and leaves the
    env flag on disk, #73289)."""
    raw = _getenv("WHATSAPP_ENABLED")  # None = unset; "" and " " are SET
    wa_cfg = config.platforms.get(Platform.WHATSAPP)
    # Any SET non-truthy value (``off``, `` false ``, empty) is the opt-out, the same rule ``_is_connected``
    # applies; only an unset flag leaves the decision to config.yaml.
    if wa_cfg is not None and raw is not None and not is_truthy_value(raw):
        wa_cfg.enabled = False
    elif is_truthy_value(raw):
        enable_from_env(config, Platform.WHATSAPP)


ENV_ENABLE_CREDENTIALS = ("WHATSAPP_ENABLED",)
ENV_OVERRIDES = (_enabled_flag, home_step(Platform.WHATSAPP, "WHATSAPP_HOME_CHANNEL"))

# Authorization env names (``PlatformEntry`` fields). Core reads them from this file when the
# live entry lacks them: a user plugin overriding this platform, safe mode, the plugin disabled.
AUTHZ = {
    "allowed_users_env": "WHATSAPP_ALLOWED_USERS",
    "allow_all_env": "WHATSAPP_ALLOW_ALL_USERS",
    "own_policy_env": ("WHATSAPP_DM_POLICY", "WHATSAPP_GROUP_POLICY"),
    "allowlist_suppresses_pairing": True,
}
