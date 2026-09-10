"""Create a private per-computer configuration without putting a token in shell history."""
import argparse
import base64
import getpass
import os
from pathlib import Path
import urllib.parse

from client import dpapi, write_json
from configuration import default_client_config_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(default_client_config_path()))
    parser.add_argument('--url', required=True, help='HTTPS relay origin, optionally with a port')
    parser.add_argument('--device', required=True, help='This device name as configured on the server')
    parser.add_argument('--target', action='append', required=True, help='Destination device; repeat for multiple recipients')
    parser.add_argument('--source-database', help='Existing AI Twin database, when record export is enabled')
    parser.add_argument('--export-existing-records', action='store_true', help='Enable the Windows app record adapter')
    parser.add_argument('--force', action='store_true', help='Replace an existing local configuration')
    args = parser.parse_args()
    parsed = urllib.parse.urlsplit(args.url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        parser.error('--url must be an HTTPS origin without a path or credentials')
    if args.export_existing_records and os.name != 'nt':
        parser.error('Existing application records require Windows DPAPI under the original Windows user')
    path = Path(args.config).expanduser().resolve()
    if path.exists() and not args.force:
        parser.error('Configuration already exists; use --force to replace it')
    token = getpass.getpass('Device token (hidden): ').strip()
    if not token or len(token) > 249 or any(char.isspace() for char in token):
        parser.error('Device token must contain 1–249 non-whitespace characters')
    config = {'url': args.url.rstrip('/'), 'device': args.device, 'targets': args.target,
              'export_existing_records': args.export_existing_records}
    if args.source_database:
        config['source_database'] = str(Path(args.source_database).expanduser().resolve())
    if os.name == 'nt':
        config['token_dpapi'] = base64.b64encode(dpapi(token.encode())).decode('ascii')
        write_json(path, config)
    else:
        config['token'] = token
        old_mask = os.umask(0o077)
        try:
            write_json(path, config)
            path.chmod(0o600)
        finally:
            os.umask(old_mask)
    print(f'Configuration saved to {path}')


if __name__ == '__main__':
    main()
