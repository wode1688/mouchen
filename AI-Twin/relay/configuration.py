"""Per-user client configuration, kept outside the source checkout by default."""
import json
import os
from pathlib import Path


def local_data_dir():
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    return Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share')


def default_state_dir():
    return local_data_dir() / 'Mouchen' / 'Relay'


def default_client_config_path():
    override = os.environ.get('AI_TWIN_RELAY_CONFIG')
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    return default_state_dir() / 'client-config.json'


def load_client_config(path=None):
    path = Path(path or default_client_config_path()).expanduser().resolve()
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(config, dict):
        raise ValueError('Client configuration must be a JSON object')

    def resolved(value):
        result = Path(os.path.expandvars(str(value))).expanduser()
        return str((result if result.is_absolute() else path.parent / result).resolve())

    config['state_dir'] = resolved(config.get('state_dir') or default_state_dir())
    state = Path(config['state_dir'])
    for key, fallback in {
        'archive_dir': state / 'Archive',
        'outbox_dir': state / 'Outbox',
        'source_database': local_data_dir() / 'Mouchen' / 'Desktop' / 'mouchen-desktop.db',
    }.items():
        config[key] = resolved(config.get(key) or fallback)
    # Importing existing app records is an explicit per-computer choice.
    config.setdefault('export_existing_records', False)
    return config
