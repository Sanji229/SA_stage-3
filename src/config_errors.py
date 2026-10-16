"""Configuration exceptions shared without importing configuration values."""


class ConfigError(RuntimeError):
    """Required service configuration is missing or unsafe."""


class SettingsError(ConfigError):
    """A configured application setting is malformed or outside its safe range."""
